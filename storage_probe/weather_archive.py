"""Private open Parquet/DuckDB weather archive.

Immutable transfer receipts remain the transaction boundary.  The archive is
parameter-agnostic: provider fields are stored in a long table, so adding a
weather parameter never requires adding a Parquet column.  The complete native
record is retained losslessly for replay/audit and existing forecast consumers
remain on canonical-model-record-v1 until an explicit cutover.
"""
import hashlib
import json
from pathlib import Path

from full_horizon_contract import validate_archive, utc

VERSION = "mardorf-weather-archive-v3"
READABLE_VERSIONS = {"mardorf-weather-archive-v1", "mardorf-weather-archive-v2", VERSION}
DELTA_POLICY_VERSION = "weather-archive-delta-policy-v1"


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _family(model):
    if model.startswith("ICON-"):
        return "DWD-ICON"
    if model in ("GFS", "GEFS-control"):
        return "GFS"
    return "ECMWF"


def _member(model, row):
    return row.get("member_id") or ("control" if model == "GEFS-control" else None)

def _grid_identity(row):
    point = row.get("forecast_coordinate_or_grid_point") or {}
    grid_id = row.get("grid_id") or point.get("grid_id")
    return {
        "grid_id": grid_id,
        "latitude": point.get("latitude", point.get("lat")),
        "longitude": point.get("longitude", point.get("lon")),
    }


def logical_record_identity(model, row):
    grid = _grid_identity(row)
    identity = {
        "model": model,
        "member_id": _member(model, row),
        "run_time_utc": utc(row["run_time_utc"]).isoformat(),
        "valid_time_utc": utc(row["valid_time_utc"]).isoformat(),
        "provider_product": row.get("provider_product", "legacy_native_product"),
        "grid_id": grid["grid_id"],
        "latitude": None if grid["latitude"] is None else round(float(grid["latitude"]), 6),
        "longitude": None if grid["longitude"] is None else round(float(grid["longitude"]), 6),
    }
    return digest(compact(identity).encode())


def _revision_payload(row):
    """Remove retrieval-only fields while preserving provider/content revisions."""
    out = json.loads(json.dumps(row))
    out.pop("retrieved_at_utc", None)
    return out


def revision_sha256(row):
    return digest(compact(_revision_payload(row)).encode())


def _lead_is_retained(lead, terminal):
    lead = int(lead)
    if lead <= 72:
        keep = lead % 3 == 0
    elif lead <= 120:
        keep = lead % 6 == 0
    elif lead <= 240:
        keep = lead % 12 == 0
    else:
        keep = lead % 24 == 0
    return keep or (terminal is not None and lead == int(terminal))


def _cycle_is_retained(run, lead):
    hour = run.hour
    if lead <= 48:
        return True
    if lead <= 120:
        return hour in (0, 6, 12, 18)
    if lead <= 384:
        return hour in (0, 12)
    return hour == 0


def retention_decision(model, row, terminal=None):
    lead = int(row["forecast_lead_hours"])
    run = utc(row["run_time_utc"])
    cycle_ok = _cycle_is_retained(run, lead)
    lead_ok = _lead_is_retained(lead, terminal)
    return {
        "retain": bool(cycle_ok and lead_ok),
        "cycle_retained": bool(cycle_ok),
        "lead_retained": bool(lead_ok),
        "policy_version": DELTA_POLICY_VERSION,
    }


def iter_payload_records(payload):
    for model, core in payload.get("models", {}).items():
        source = payload.get("full_horizon_archive", {}).get("sources", {}).get(model, {})
        terminal = source.get("expected_max_lead_for_cycle", source.get("target_max_hours"))
        for row in list(core) + list(source.get("records", [])):
            yield model, row, terminal


def _existing_revision_keys(root):
    """Read logical/revision identities from all manifest-authorized fragments."""
    import pyarrow.parquet as pq
    root = Path(root)
    keys = set()
    manifests = root / "data/weather_archive/manifests"
    if not manifests.exists():
        return keys
    for manifest_path in manifests.glob("year=*/month=*/day=*/*.json"):
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("schema_version") not in READABLE_VERSIONS:
            raise ValueError("archive manifest version mismatch")
        for item in manifest.get("files", []):
            if item.get("dataset") != "forecast_records":
                continue
            path = root / item["path"]
            if digest(path.read_bytes()) != item["sha256"]:
                raise ValueError("archive checksum mismatch while building delta index")
            table = pq.ParquetFile(path).read()
            columns = set(table.column_names)
            if {"logical_record_id", "revision_sha256"} <= columns:
                logical = table.column("logical_record_id").to_pylist()
                revisions = table.column("revision_sha256").to_pylist()
                for a, b in zip(logical, revisions):
                    if a and b:
                        keys.add((a, b))
                # Mixed legacy/v3 fragments are not expected inside one file.
                if all(a and b for a, b in zip(logical, revisions)):
                    continue
            models = table.column("model").to_pylist()
            raw = table.column("record_json").to_pylist()
            for model, text in zip(models, raw):
                row = json.loads(text)
                keys.add((logical_record_identity(model, row), revision_sha256(row)))
    return keys


def _update_cycle_evidence(root, payload, provenance):
    root = Path(root)
    seen = {}
    for model, row, _terminal in iter_payload_records(payload):
        run = utc(row["run_time_utc"])
        seen[(model, run)] = True
    paths = []
    observed = utc(provenance["collector_generated_at_utc"])
    for model, run in sorted(seen):
        safe_model = model.lower().replace("_", "-")
        path = root / "data/weather_archive/cycle_evidence" / safe_model / run.strftime("year=%Y/month=%m/day=%d") / ("run=" + run.strftime("%Y%m%dT%H%M%SZ") + ".json")
        previous = {}
        if path.exists():
            previous = json.loads(path.read_text())
        same_payload = previous.get("last_payload_sha256") == provenance["payload_sha256"]
        count = int(previous.get("retrieval_count", 0)) + (0 if same_payload else 1)
        evidence = {
            "schema_version": 1,
            "method_version": DELTA_POLICY_VERSION,
            "model": model,
            "run_time_utc": run.isoformat(),
            "first_seen_at_utc": previous.get("first_seen_at_utc") or observed.isoformat(),
            "last_seen_at_utc": previous.get("last_seen_at_utc") if same_payload else observed.isoformat(),
            "retrieval_count": count,
            "first_payload_sha256": previous.get("first_payload_sha256") or provenance["payload_sha256"],
            "last_payload_sha256": provenance["payload_sha256"],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(compact(evidence) + "\n")
        tmp.replace(path)
        paths.append(path.relative_to(root).as_posix())
    return paths



def _scalar(item):
    """Return stable open scalar representation without provider coercion."""
    value = item.get("value") if isinstance(item, dict) else item
    if value is None:
        return "null", None, None, None
    if isinstance(value, bool):
        return "boolean", None, None, value
    if isinstance(value, (int, float)):
        return "number", float(value), None, None
    if isinstance(value, str):
        return "string", None, value, None
    # Non-scalars stay losslessly in field_metadata_json/record_json.  Keeping
    # them out of typed scalar columns avoids silently inventing semantics.
    return "json", None, compact(value), None


def make_tables(payload, provenance, existing_revision_keys=None):
    coverage = validate_archive(payload)
    existing_revision_keys = set(existing_revision_keys or ())
    record_rows, value_rows = [], []
    stats = {"input_records": 0, "policy_filtered_records": 0, "duplicate_revisions_skipped": 0, "new_revision_records": 0}
    for model, row, terminal in iter_payload_records(payload):
            stats["input_records"] += 1
            decision = retention_decision(model, row, terminal)
            if not decision["retain"]:
                stats["policy_filtered_records"] += 1
                continue
            logical_id = logical_record_identity(model, row)
            revision = revision_sha256(row)
            if (logical_id, revision) in existing_revision_keys:
                stats["duplicate_revisions_skipped"] += 1
                continue
            raw = compact(row)
            record_id = digest((logical_id + ":" + revision).encode())
            point = row.get("forecast_coordinate_or_grid_point") or {}
            base = {
                "record_id": record_id,
                "logical_record_id": logical_id,
                "revision_sha256": revision,
                "model": model,
                "model_family": _family(model),
                "member_id": _member(model, row),
                "run_time_utc": utc(row["run_time_utc"]),
                "valid_time_utc": utc(row["valid_time_utc"]),
                "lead_hours": float(row["forecast_lead_hours"]),
                "available_at_utc": utc(row.get("retrieved_at_utc") or payload["retrieved_at_utc"]),
                "latitude": point.get("latitude", point.get("lat")),
                "longitude": point.get("longitude", point.get("lon")),
                "provider_product": row.get("provider_product", "legacy_native_product"),
                "source_payload_sha256": provenance["payload_sha256"],
            }
            record_rows.append({**base, "record_json": raw})
            # Any provider/derived parameter is represented as another row.
            # No allow-list exists here by design.
            for namespace in ("values", "derived"):
                for parameter, items in (row.get(namespace) or {}).items():
                    items = items if isinstance(items, list) else [items]
                    for index, original in enumerate(items):
                        item = original if isinstance(original, dict) else {"value": original}
                        kind, number, text, boolean = _scalar(item)
                        units = item.get("units") or item.get("unit")
                        if namespace == "derived" and not units:
                            units = "m/s" if parameter.endswith("_ms") else "kt" if parameter.endswith("_kt") else "degree" if parameter.endswith("_deg") else "1" if parameter == "gust_factor" else None
                        value_rows.append({
                            **base,
                            "parameter_native": str(parameter),
                            "namespace": namespace,
                            "value_index": index,
                            "type_of_level_native": str(item["typeOfLevel"]) if "typeOfLevel" in item else None,
                            "level_native": str(item["level"]) if "level" in item else None,
                            "step_type_native": str(item["stepType"]) if "stepType" in item else None,
                            "value_kind": kind,
                            "value_native": number,
                            "value_text_native": text,
                            "value_bool_native": boolean,
                            "unit_native": units,
                            "step_range_native": str(item["stepRange"]) if "stepRange" in item else None,
                            "normalization_status": "derived_existing" if namespace == "derived" else "provider_native_unconverted",
                            "field_metadata_json": compact(item),
                        })
            stats["new_revision_records"] += 1
            existing_revision_keys.add((logical_id, revision))
    stats["retained_candidate_records"] = stats["new_revision_records"] + stats["duplicate_revisions_skipped"]
    return record_rows, value_rows, coverage, stats


def schemas():
    import pyarrow as pa
    base = [
        ("record_id", pa.string()), ("logical_record_id", pa.string()), ("revision_sha256", pa.string()),
        ("model", pa.string()), ("model_family", pa.string()),
        ("member_id", pa.string()), ("run_time_utc", pa.timestamp("us", tz="UTC")),
        ("valid_time_utc", pa.timestamp("us", tz="UTC")), ("lead_hours", pa.float64()),
        ("available_at_utc", pa.timestamp("us", tz="UTC")), ("latitude", pa.float64()),
        ("longitude", pa.float64()), ("provider_product", pa.string()), ("source_payload_sha256", pa.string()),
    ]
    metadata = {b"schema_version": VERSION.encode(), b"layout": b"open-long-parameter-table"}
    return {
        "forecast_records": pa.schema(base + [("record_json", pa.string())], metadata=metadata),
        "weather_values": pa.schema(base + [
            ("parameter_native", pa.string()), ("namespace", pa.string()), ("value_index", pa.int32()),
            ("type_of_level_native", pa.string()), ("level_native", pa.string()), ("step_type_native", pa.string()),
            ("value_kind", pa.string()), ("value_native", pa.float64()), ("value_text_native", pa.string()),
            ("value_bool_native", pa.bool_()), ("unit_native", pa.string()), ("step_range_native", pa.string()),
            ("normalization_status", pa.string()), ("field_metadata_json", pa.string()),
        ], metadata=metadata),
    }


def persist_archive(root, payload, provenance):
    import pyarrow as pa
    import pyarrow.parquet as pq
    root = Path(root)
    payload_hash = provenance["payload_sha256"]
    if len(payload_hash) != 64 or any(c not in "0123456789abcdef" for c in payload_hash):
        raise ValueError("invalid source payload hash")
    existing = _existing_revision_keys(root)
    records, values, coverage, delta = make_tables(payload, provenance, existing)
    when = utc(provenance["collector_generated_at_utc"])
    partition = when.strftime("year=%Y/month=%m/day=%d")
    folder = root / "data/weather_archive"
    manifest_path = folder / "manifests" / partition / (payload_hash + ".json")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        for item in manifest["files"]:
            if digest((root / item["path"]).read_bytes()) != item["sha256"]:
                raise ValueError("archive readback checksum mismatch")
        return manifest
    files = []
    for name, rows in (("forecast_records", records), ("weather_values", values)):
        if not rows:
            continue
        path = folder / name / partition / (payload_hash + ".parquet")
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(rows, schema=schemas()[name])
        tmp = path.with_suffix(".tmp")
        pq.write_table(table, tmp, compression="zstd", use_dictionary=True, row_group_size=65536)
        restored = pq.ParquetFile(tmp).read()
        if not restored.equals(table):
            raise ValueError("Parquet roundtrip mismatch")
        tmp.replace(path)
        files.append({"dataset": name, "path": path.relative_to(root).as_posix(),
                      "sha256": digest(path.read_bytes()), "rows": len(rows), "bytes": path.stat().st_size})
    evidence_paths = _update_cycle_evidence(root, payload, provenance)
    if not records:
        return {
            "schema_version": VERSION,
            "storage_layout": "open-long-parameter-table",
            "source_payload_sha256": payload_hash,
            "source_payload_path": provenance["payload_path"],
            "transfer_receipt": provenance["receipt_path"],
            "collector_generated_at_utc": provenance["collector_generated_at_utc"],
            "coverage": coverage,
            "delta_policy_version": DELTA_POLICY_VERSION,
            "delta": delta,
            "cycle_evidence_paths": evidence_paths,
            "manifest_persisted": False,
            "files": [],
        }
    manifest = {
        "schema_version": VERSION,
        "storage_layout": "open-long-parameter-table",
        "source_payload_sha256": payload_hash,
        "source_payload_path": provenance["payload_path"],
        "transfer_receipt": provenance["receipt_path"],
        "collector_generated_at_utc": provenance["collector_generated_at_utc"],
        "coverage": coverage,
        "delta_policy_version": DELTA_POLICY_VERSION,
        "delta": delta,
        "cycle_evidence_paths": evidence_paths,
        "manifest_persisted": True,
        "files": files,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(compact(manifest) + "\n")
    tmp.replace(manifest_path)
    return manifest


def connect_archive(root, as_of=None):
    """Expose checksum-verified v1-v3 fragments through schema-union DuckDB views."""
    import duckdb
    root = Path(root)
    inventory = {"forecast_records": set(), "weather_values": set()}
    for path in (root / "data/weather_archive/manifests").glob("year=*/month=*/day=*/*.json"):
        manifest = json.loads(path.read_text())
        if manifest["schema_version"] not in READABLE_VERSIONS:
            raise ValueError("archive manifest version mismatch")
        for item in manifest["files"]:
            file = (root / item["path"]).resolve()
            if not file.is_relative_to((root / "data/weather_archive").resolve()):
                raise ValueError("archive manifest path escape")
            if digest(file.read_bytes()) != item["sha256"]:
                raise ValueError("archive checksum mismatch")
            inventory[item["dataset"]].add(str(file))
    con = duckdb.connect(":memory:")
    for name, files in inventory.items():
        if files:
            relation = con.read_parquet(sorted(files), hive_partitioning=False, union_by_name=True)
        else:
            import pyarrow as pa
            relation = con.from_arrow(pa.Table.from_pylist([], schema=schemas()[name]))
        relation.create_view(name + "_all")
        columns = set(relation.columns)
        cutoff = "" if as_of is None else "WHERE available_at_utc <= TIMESTAMPTZ '" + utc(as_of).isoformat() + "'"
        record_identity = "model, coalesce(member_id,''), run_time_utc, valid_time_utc, provider_product, latitude, longitude"
        if name == "forecast_records":
            keys = record_identity
            con.execute(f"CREATE VIEW {name} AS SELECT * FROM {name}_all {cutoff} QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY available_at_utc DESC, source_payload_sha256 DESC)=1")
        else:
            # Parameter rows must belong to the selected record revision.  This
            # prevents a field removed by a later provider revision from leaking
            # forward from an older revision.
            con.execute(
                "CREATE VIEW weather_values AS "
                "SELECT w.* FROM weather_values_all w "
                "INNER JOIN forecast_records f ON w.record_id = f.record_id"
            )
    return con
