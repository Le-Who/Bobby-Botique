from scripts.check_docs_links import check_document, headings


def test_inline_code_duplicate_headings_and_encoded_paths(tmp_path):
    target = tmp_path / "my guide.md"
    target.write_text("# Use `config`\n# Use config\n# Use config-1\n", encoding="utf-8")
    assert headings(target) >= {"use-config", "use-config-1", "use-config-1-1"}
    doc = tmp_path / "README.md"
    doc.write_text("[ok](my%20guide.md#use-config)\n[again](<my guide.md#use-config-1>)\n", encoding="utf-8")
    assert check_document(doc, tmp_path) == []


def test_negative_links_reference_links_and_fenced_examples(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text(
        "# Title\n[bad](#missing)\n[ref][guide]\n\n[guide]: absent.md\n````md\n```\n[example](not-real.md)\n````\n",
        encoding="utf-8",
    )
    errors = check_document(doc, tmp_path)
    assert len(errors) == 2
    assert any("missing" in error for error in errors)
    assert any("absent.md" in error for error in errors)


def test_explicit_anchor_and_repository_root_link(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text('<a id="custom"></a>\n[ok](#custom)\n[also](/README.md#custom)\n', encoding="utf-8")
    assert check_document(doc, tmp_path) == []
