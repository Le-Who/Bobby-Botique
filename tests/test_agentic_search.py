import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from google.genai import types
from app.core.agentic import AgenticSearch

@pytest.fixture
def mock_agent():
    with patch("app.core.agentic.genai.Client"):
        agent = AgenticSearch(model_name="gemini-2.5-pro", api_key="fake-key")
        # Override client.aio.models.generate_content for testing
        agent.client = MagicMock()
        agent.client.aio = AsyncMock()
        agent.client.aio.models.generate_content = AsyncMock()
        return agent

@pytest.fixture
def mock_status_callback():
    return AsyncMock()

@pytest.mark.asyncio
async def test_agentic_search_direct_answer(mock_agent, mock_status_callback):
    """Test when the model answers directly without using any tools."""
    # Setup mock response without function calls
    mock_response = MagicMock()
    mock_response.candidates = [
        MagicMock(content=MagicMock(parts=[MagicMock(function_call=None, text="Direct answer")]))
    ]
    mock_agent.client.aio.models.generate_content.return_value = mock_response

    result = await mock_agent.run("What is 2+2?", mock_status_callback)

    assert result == "Direct answer"
    mock_agent.client.aio.models.generate_content.assert_called_once()
    mock_status_callback.assert_called_with("Планирую исследование...")

@pytest.mark.asyncio
async def test_agentic_search_conclude_tool(mock_agent, mock_status_callback):
    """Test when the model uses the conclude_research tool."""
    # Setup mock response with conclude_research
    call_mock = MagicMock()
    call_mock.name = "conclude_research"
    call_mock.args = {"answer": "Concluded answer"}
    
    part_mock = MagicMock()
    part_mock.function_call = call_mock
    part_mock.text = None

    mock_response = MagicMock()
    mock_response.candidates = [MagicMock(content=MagicMock(parts=[part_mock]))]
    
    mock_agent.client.aio.models.generate_content.return_value = mock_response

    result = await mock_agent.run("Conclude this", mock_status_callback)

    assert result == "Concluded answer"
    mock_agent.client.aio.models.generate_content.assert_called_once()

@pytest.mark.asyncio
async def test_agentic_search_tool_loop(mock_agent, mock_status_callback):
    """Test a loop where the model searches, then concludes."""
    
    # First response: call search_web
    search_call = MagicMock()
    search_call.name = "search_web"
    search_call.args = {"queries": ["test query"]}
    search_part = MagicMock()
    search_part.function_call = search_call
    search_part.text = None
    
    resp1 = MagicMock()
    resp1.candidates = [MagicMock(content=MagicMock(parts=[search_part]))]

    # Second response: call conclude_research
    conclude_call = MagicMock()
    conclude_call.name = "conclude_research"
    conclude_call.args = {"answer": "Final synthesized answer"}
    conclude_part = MagicMock()
    conclude_part.function_call = conclude_call
    conclude_part.text = None
    
    resp2 = MagicMock()
    resp2.candidates = [MagicMock(content=MagicMock(parts=[conclude_part]))]

    mock_agent.client.aio.models.generate_content.side_effect = [resp1, resp2]

    with patch("app.core.agentic.parallel_search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = [{"url": "http://test.com", "content": "Test data"}]
        
        result = await mock_agent.run("Do a search", mock_status_callback)
        
        assert result == "Final synthesized answer"
        assert mock_agent.client.aio.models.generate_content.call_count == 2
        mock_search.assert_called_once_with(["test query"], user_id=None, chat_id=None, max_results=10)

@pytest.mark.asyncio
async def test_agentic_search_max_pages_enforcement(mock_agent, mock_status_callback):
    """Test that max pages limit is strictly enforced."""
    mock_agent.max_pages = 1
    mock_agent.max_iterations = 3
    
    # First response: read page 1 and page 2 in parallel
    read_call1 = MagicMock()
    read_call1.name = "read_page"
    read_call1.args = {"url": "http://page1.com"}
    part1 = MagicMock(function_call=read_call1, text=None)
    
    read_call2 = MagicMock()
    read_call2.name = "read_page"
    read_call2.args = {"url": "http://page2.com"}
    part2 = MagicMock(function_call=read_call2, text=None)
    
    resp1 = MagicMock()
    resp1.candidates = [MagicMock(content=MagicMock(parts=[part1, part2]))]

    # Second response: conclude
    conclude_call = MagicMock()
    conclude_call.name = "conclude_research"
    conclude_call.args = {"answer": "Done reading"}
    part3 = MagicMock(function_call=conclude_call, text=None)
    
    resp2 = MagicMock()
    resp2.candidates = [MagicMock(content=MagicMock(parts=[part3]))]

    mock_agent.client.aio.models.generate_content.side_effect = [resp1, resp2]

    with patch("app.core.agentic.read_url", new_callable=AsyncMock) as mock_read:
        mock_read.return_value = "Page content"
        
        result = await mock_agent.run("Read two pages", mock_status_callback)
        
        assert result == "Done reading"
        # read_url should only be called ONCE because the second call hits the limit
        mock_read.assert_called_once_with("http://page1.com", timeout=12.0)
