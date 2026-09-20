import unittest
from datetime import datetime, timezone

from finalize_secondary_batch import build_batch, compact_stamp

UTC=timezone.utc


def report(kind,generated,sha):
    return {
        "kind":kind,
        "generated_at_utc":generated,
        "status":"PASS",
        "error_count":0,
        "bundle_ready_for_private_revalidation":True,
        "input_payload_sha256":sha,
    }


def receipt(kind,generated,sha):
    return {
        "schema_version":2,
        "method_version":"private-transfer-readback-v2",
        "kind":kind,
        "stamp":compact_stamp(generated),
        "source_generated_at_utc":generated,
        "verified_at_utc":generated,
        "verified_data_commit_sha":"deadbeef-"+kind,
        "readback_verified":True,
        "payload_source_sha256":sha,
        "audit_input_payload_sha256":sha,
        "payload_destination":f"data/inbox/public_collector/{kind}/payload.json.gz",
    }


class SecondaryBatchTests(unittest.TestCase):
    def setUp(self):
        self.w="2026-09-20T13:53:31.584051+00:00"
        self.e="2026-09-20T13:53:32.262033+00:00"
        self.reports={
            "wunstorf":report("wunstorf",self.w,"w"*64),
            "etnw":report("etnw",self.e,"e"*64),
        }
        self.receipts={
            "wunstorf":receipt("wunstorf",self.w,"w"*64),
            "etnw":receipt("etnw",self.e,"e"*64),
        }

    def test_complete_batch_binds_both_verified_children(self):
        b=build_batch(
            self.reports,self.receipts,
            now=datetime(2026,9,20,14,tzinfo=UTC),
            run_id="123",run_attempt="1",
        )
        self.assertTrue(b["complete"])
        self.assertEqual(b["kind"],"secondary")
        self.assertEqual(b["source_generated_at_utc"],self.e)
        self.assertEqual(set(b["children"]),{"wunstorf","etnw"})
        self.assertEqual(
            b["children"]["wunstorf"]["receipt_path"],
            "data/inbox/public_collector/transfer_receipts/wunstorf/2026/09/20/"
            f"receipt_{compact_stamp(self.w)}.json",
        )

    def test_mismatched_child_attempt_is_rejected(self):
        self.receipts["etnw"]["stamp"]="wrong"
        with self.assertRaisesRegex(RuntimeError,"child receipt mismatch"):
            build_batch(self.reports,self.receipts)

    def test_nonready_child_blocks_batch(self):
        self.reports["wunstorf"]["bundle_ready_for_private_revalidation"]=False
        with self.assertRaisesRegex(RuntimeError,"not ready"):
            build_batch(self.reports,self.receipts)

    def test_child_payload_sha_mismatch_blocks_batch(self):
        self.receipts["wunstorf"]["payload_source_sha256"]="x"*64
        with self.assertRaisesRegex(RuntimeError,"payload SHA"):
            build_batch(self.reports,self.receipts)


if __name__=="__main__":
    unittest.main()
