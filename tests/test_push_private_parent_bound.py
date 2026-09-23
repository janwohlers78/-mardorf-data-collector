import base64
import json
import unittest
from unittest.mock import patch

import push_private as pp


class FakePatchResponse:
    ok = True
    status_code = 200
    text = ""


class ParentBoundTransferTests(unittest.TestCase):
    def test_atomic_commit_binds_guards_and_exact_checks_to_parent(self):
        item={
            "path":"data/inbox/public_collector/integrity/models/latest.json",
            "content":b'{"generated_at_utc":"2026-09-23T09:00:00+00:00"}\n',
            "immutable":False,
            "monotonic_guard":{
                "path":"data/inbox/public_collector/integrity/models/latest.json",
                "field":"generated_at_utc",
                "incoming_time":"2026-09-23T09:00:00+00:00",
            },
        }
        calls=[]
        def fake_req(method,url,h,**kwargs):
            calls.append((method,url))
            if method=="GET" and url.endswith("/git/ref/heads/main"):
                return {"object":{"sha":"parent-3" if len([x for x in calls if x[1].endswith('/git/ref/heads/main')])==1 else "commit-new"}}
            if method=="GET" and url.endswith("/git/commits/parent-3"):
                return {"tree":{"sha":"tree-parent"}}
            if method=="POST" and url.endswith("/git/trees"):
                return {"sha":"tree-new"}
            if method=="POST" and url.endswith("/git/commits"):
                return {"sha":"commit-new"}
            raise AssertionError((method,url,kwargs))

        def guard(repo,item,h,ref=None):
            self.assertEqual(ref,"parent-3")
            self.assertTrue(any(u.endswith("/git/ref/heads/main") for _,u in calls))
            return True

        def exact(repo,item,h,ref=None):
            self.assertEqual(ref,"parent-3")
            return False

        with patch.object(pp,"blob",return_value="blob-new"), \
             patch.object(pp,"req",side_effect=fake_req), \
             patch.object(pp,"monotonic_allows",side_effect=guard) as guard_mock, \
             patch.object(pp,"mutable_already_exact",side_effect=exact) as exact_mock, \
             patch.object(pp,"verify_unpublished_commit",return_value=[]), \
             patch.object(pp,"descendant_preserves",return_value=True), \
             patch.object(pp.requests,"patch",return_value=FakePatchResponse()):
            result=pp.atomic_commit("owner/private",[item],"test",{})

        self.assertEqual(result["parent_sha"],"parent-3")
        self.assertEqual(result["commit_sha"],"commit-new")
        self.assertEqual(guard_mock.call_count,1)
        self.assertEqual(exact_mock.call_count,1)

    def test_immutable_collision_check_is_parent_bound(self):
        item={"path":"immutable.json","content":b"x","immutable":True}
        def fake_req(method,url,h,**kwargs):
            if method=="GET" and url.endswith("/git/ref/heads/main"):
                return {"object":{"sha":"parent-fixed"}}
            raise AssertionError((method,url,kwargs))
        def same(repo,path,content,h,gz=False,ref=None):
            self.assertEqual(ref,"parent-fixed")
            return "same-blob"
        with patch.object(pp,"blob",return_value="same-blob"), \
             patch.object(pp,"req",side_effect=fake_req), \
             patch.object(pp,"same_existing",side_effect=same):
            result=pp.atomic_commit("owner/private",[item],"test",{})
        self.assertTrue(result["idempotent"])
        self.assertEqual(result["parent_sha"],"parent-fixed")

    def test_later_descendant_may_advance_monotonic_pointer(self):
        item={
            "path":"latest.json",
            "content":b'{}',
            "immutable":False,
            "monotonic_guard":{
                "path":"latest.json",
                "field":"generated_at_utc",
                "incoming_time":"2026-09-23T09:00:00+00:00",
            },
        }
        newer={"generated_at_utc":"2026-09-23T10:00:00+00:00"}
        meta={"content":base64.b64encode(json.dumps(newer).encode()).decode()}
        with patch.object(pp,"req",return_value={"status":"ahead"}), \
             patch.object(pp,"content_meta",return_value=meta) as cm:
            self.assertTrue(pp.descendant_preserves(
                "owner/private","published","new-head",[item],{}
            ))
        self.assertEqual(cm.call_args.kwargs["ref"],"new-head")

        older={"generated_at_utc":"2026-09-23T08:00:00+00:00"}
        meta={"content":base64.b64encode(json.dumps(older).encode()).decode()}
        with patch.object(pp,"req",return_value={"status":"ahead"}), \
             patch.object(pp,"content_meta",return_value=meta):
            self.assertFalse(pp.descendant_preserves(
                "owner/private","published","new-head",[item],{}
            ))


if __name__=="__main__":
    unittest.main()
