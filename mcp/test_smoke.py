"""yuh-mcp 冒烟测试：①端口发现+直连通道 ②MCP stdio 客户端全链路。"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def part1() -> bool:
    import server as srv

    try:
        port = await srv.get_port(force=True)
        print(f"[P1] PORT={port}")
    except Exception as exc:
        print(f"[P1] 端口发现失败: {exc}")
        return False
    for channel in ("system:usage", "tasks:list", "providers:list"):
        try:
            result = await srv.call_studio(channel)
            text = json.dumps(result, ensure_ascii=False, default=str)
            print(f"[P1] {channel} -> {text[:200]}")
        except Exception as exc:
            print(f"[P1] {channel} -> 失败: {exc}")
    try:
        nodes = await srv.call_studio("canvas:list", {})
        n = len(nodes) if isinstance(nodes, list) else "?"
        print(f"[P1] canvas:list -> {n} 个节点")
    except Exception as exc:
        print(f"[P1] canvas:list -> 失败: {exc}")
    return True


async def part2() -> bool:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    python = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "Scripts", "python.exe")
    server_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
    params = StdioServerParameters(command=python, args=["-X", "utf8", server_py])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"[P2] MCP 连接成功，工具数={len(names)}")
            print("[P2] 工具清单:", ", ".join(names))
            for tool_name, args in (
                ("yuh_status", {}),
                ("yuh_list_tasks", {}),
                ("yuh_canvas_list", {}),
            ):
                result = await session.call_tool(tool_name, args)
                text = (result.content[0].text if result.content else "")[:300]
                print(f"[P2] {tool_name} -> {text}")
            return True


if __name__ == "__main__":
    ok1 = asyncio.run(part1())
    if ok1:
        asyncio.run(part2())
    else:
        print("P1 失败，跳过 P2")
