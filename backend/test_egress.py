"""Guards for the Supabase egress slimming."""

from egress_meter import record_http, record_job, record_supabase, reset, snapshot
from db import EMAIL_DETAIL_WITH_ANALYSIS, EMAIL_LIST_WITH_ANALYSIS


def test_list_select_omits_message_body():
    assert "body" not in EMAIL_LIST_WITH_ANALYSIS
    assert "body" in EMAIL_DETAIL_WITH_ANALYSIS
    assert "summary" in EMAIL_LIST_WITH_ANALYSIS


def test_meter_labels_the_operation():
    from egress_meter import using_operation

    reset()
    with using_operation("inbox_list"):
        record_supabase("emails", 20, 4000)
    stats = snapshot()
    assert stats["supabase_to_backend"]["inbox_list:emails"]["bytes"] == 4000
    assert stats["top_operations"][0]["operation"] == "inbox_list:emails"
    assert stats["scheduler_instance"]


def test_meter_separates_supabase_and_browser_bytes():
    reset()
    record_supabase("emails", 40, 2_000_000)
    record_http("/db/conversations", 12_000)
    record_job("gmail_inbox_sync")
    stats = snapshot()
    assert stats["supabase_to_backend"]["emails"]["bytes"] == 2_000_000
    assert stats["supabase_to_backend"]["emails"]["rows"] == 40
    assert stats["backend_to_browser"]["/db/conversations"]["bytes"] == 12_000
    assert stats["jobs"]["gmail_inbox_sync"]["runs"] == 1
    assert stats["supabase_to_backend_bytes"] == 2_000_000
    assert stats["backend_to_browser_bytes"] == 12_000


if __name__ == "__main__":
    test_list_select_omits_message_body()
    test_meter_labels_the_operation()
    test_meter_separates_supabase_and_browser_bytes()
    print("egress tests passed")
