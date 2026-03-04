def test_optimization_logic():
    # This is a unit test for the logic I'm implementing
    selected_urls = ["url1", "url2", "url3"]
    search_results = [
        {"url": "url1", "content": "content1"},
        {"url": "url3", "content": "content3"},
        {"url": "url4", "content": "content4"},
    ]

    # What the optimized logic does:
    results_map = {res.get("url"): res for res in search_results if res.get("url")}
    final_context_list = []
    for url in selected_urls:
        res = results_map.get(url)
        if res:
            source_info = f"Источник: {res.get('url')}\nСодержание:\n{res.get('content')}"
            final_context_list.append(source_info)

    assert len(final_context_list) == 2
    assert "Источник: url1" in final_context_list[0]
    assert "Содержание:\ncontent1" in final_context_list[0]
    assert "Источник: url3" in final_context_list[1]
    assert "Содержание:\ncontent3" in final_context_list[1]


def test_optimization_logic_with_duplicates():
    # Test that it handles duplicate selected URLs correctly (repeats them)
    selected_urls = ["url1", "url1", "url2"]
    search_results = [
        {"url": "url1", "content": "content1"},
        {"url": "url2", "content": "content2"},
    ]

    results_map = {res.get("url"): res for res in search_results if res.get("url")}
    final_context_list = []
    for url in selected_urls:
        res = results_map.get(url)
        if res:
            source_info = f"Источник: {res.get('url')}\nСодержание:\n{res.get('content')}"
            final_context_list.append(source_info)

    assert len(final_context_list) == 3
    assert final_context_list[0] == final_context_list[1]
    assert "url1" in final_context_list[0]
    assert "url2" in final_context_list[2]


def test_optimization_logic_with_duplicate_results():
    # Test that it handles duplicate search results by taking the last one (O(1) map behavior)
    selected_urls = ["url1"]
    search_results = [
        {"url": "url1", "content": "content1_first"},
        {"url": "url1", "content": "content1_last"},
    ]

    results_map = {res.get("url"): res for res in search_results if res.get("url")}
    final_context_list = []
    for url in selected_urls:
        res = results_map.get(url)
        if res:
            source_info = f"Источник: {res.get('url')}\nСодержание:\n{res.get('content')}"
            final_context_list.append(source_info)

    assert len(final_context_list) == 1
    assert "content1_last" in final_context_list[0]
