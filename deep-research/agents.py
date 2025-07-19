from dataclasses import dataclass, field
from datetime import datetime
import html
import asyncio
import os
import aiohttp
from bs4 import BeautifulSoup
from pydantic_ai import Agent, RunContext, ModelRetry
from pydantic_ai.settings import ModelSettings
import logfire

from prompts import sub_agent_prompt, lead_agent_prompt

# Configure Logfire
logfire.configure()
logfire.instrument_pydantic_ai()
logfire.instrument_aiohttp_client()


def extract_text_content(html_content: str) -> str:
    """Extract clean readable text from HTML content using BeautifulSoup."""
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Remove unwanted elements
    for element in soup.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'menu']):
        element.decompose()
    
    # Extract clean text with proper spacing
    text = soup.get_text(separator=' ', strip=True)
    
    return text


@dataclass
class SubAgentDeps:
    brave_api_key: str
    current_date: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

sub_agent = Agent(
    model="openai:gpt-4.1-nano",
    deps_type=SubAgentDeps,
    model_settings=ModelSettings(parallel_tool_calls=True),
    instrument=True,
    retries=2
)

@sub_agent.instructions
def subagent_instruction(ctx: RunContext[SubAgentDeps]):
    return sub_agent_prompt.replace("{{.CurrentDate}}", ctx.deps.current_date)

@sub_agent.tool(retries=3)
async def web_search(
    ctx: RunContext[SubAgentDeps],
    query: str,
    count: int = 10,
    country: str = "us",
    search_lang: str = "en"
) -> str:
    """
    Search the web using Brave Search API.
    
    Args:
        query: The search query to execute
        count: Number of search results to return (1-20)
        country: Country code for localized results
        search_lang: Language for search results
        
    Returns:
        str: XML-formatted search results including query and web results
    """
    print("Running search on: ", query)
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "X-Subscription-Token": ctx.deps.brave_api_key,
                },
                params={
                    "q": query,
                    "count": count,
                    "country": country,
                    "search_lang": search_lang,
                    "result_filter": "web"
                },
            ) as response:
                # Handle HTTP error status codes
                if response.status >= 400:
                    if response.status == 429:  # Rate limited
                        await asyncio.sleep(2)
                        raise ModelRetry(f"Rate limited (429), retrying search for: {query}")
                    elif response.status >= 500:  # Server errors
                        await asyncio.sleep(3)
                        raise ModelRetry(f"Server error ({response.status}), retrying search for: {query}")
                    
                json_data = await response.json()
                
                # Extract query and results from JSON
                escaped_query = html.escape(query)
                results_xml = ""
                total_count = 0
                
                # Extract web results if they exist
                if "web" in json_data and "results" in json_data["web"]:
                    web_results = json_data["web"]["results"]
                    total_count = len(web_results)
                    
                    for result in web_results:
                        title = html.escape(result.get("title", ""))
                        url = html.escape(result.get("url", ""))
                        description = html.escape(result.get("description", ""))
                        
                        results_xml += f"""
<result>
<title>{title}</title>
<url>{url}</url>
<description>{description}</description>
</result>"""
                
                return f"""<search_result>
<query>{escaped_query}</query>
<total_count>{total_count}</total_count>
<results>{results_xml}
</results>
</search_result>"""
                
    except aiohttp.ClientError as e:
        # Network/connection errors - retry with delay
        await asyncio.sleep(2)
        raise ModelRetry(f"Network error during search, retrying: {str(e)}")
    except Exception as e:
        # For other exceptions, return error without retry
        escaped_error = html.escape(str(e))
        escaped_query = html.escape(query)
        return f"""<search_result>
<query>{escaped_query}</query>
<error>{escaped_error}</error>
</search_result>"""

@sub_agent.tool_plain
async def web_fetch(
    url: str,
    timeout: int = 30,
    headers: dict | None = None
) -> str:
    """
    Fetch content from a URL.
    
    Args:
        url: The URL to fetch content from
        timeout: Request timeout in seconds
        headers: Optional headers to include in the request
        
    Returns:
        str: XML-formatted response data including status, content, and URL
    """
    print(f"Fetching URL: {url}")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, headers=headers or {}) as response:
                html_content = await response.text()
                text_content = extract_text_content(html_content)
                escaped_content = html.escape(text_content)
                escaped_url = html.escape(str(response.url))
                return f"""<fetch_result>
<url>{escaped_url}</url>
<status_code>{response.status}</status_code>
<content>{escaped_content}</content>
</fetch_result>"""
    except Exception as e:
        escaped_error = html.escape(str(e))
        escaped_url = html.escape(url)
        return f"""<fetch_result>
<url>{escaped_url}</url>
<status_code>error</status_code>
<error>{escaped_error}</error>
</fetch_result>"""


if __name__ == "__main__":
    print(sub_agent.run_sync("Tell me about David Okpare, the AI engineer. Then also tell me about his github projects.", deps=SubAgentDeps(brave_api_key=os.getenv("BRAVE_API_KEY", "default"))).output)