from pycode.mcp.types import result_from_mcp


def test_framework_mixed_mcp_blocks_preserve_text_media_and_call_identity():
    image = {"type": "image", "mime_type": "image/png", "data": "cGl4ZWxz"}
    result = result_from_mcp(
        [
            "plain block",
            {"type": "text", "text": "text block"},
            {"type": "resource", "resource": {"uri": "file:///example", "text": "resource body"}},
            image,
        ],
        "success",
        {"structured_content": {"count": 4}},
        name="mcp__files__read",
        call_id="call-17",
        declaration={"server": "files", "original_name": "read"},
    )
    assert result.text == "plain block\ntext block\nresource body\n[image: image/png]"
    assert result.call_id == "call-17" and result.name == "mcp__files__read"
    assert result.details["mcp_content_blocks"] == [image]
    assert result.details["original_name"] == "read" and result.structured_content == {"count": 4}
