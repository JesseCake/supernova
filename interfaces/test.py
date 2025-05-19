import asyncio
from wyoming.server import AsyncTcpServer, AsyncEventHandler
from wyoming.event import Event

class DummyHandler(AsyncEventHandler):
    async def handle_event(self, event: Event) -> bool:
        print("Event received:", event)
        return True  # Return False to close connection
    
def handler_factory1(reader, writer):
    # Create a new instance for each connection, or return a singleton
    return DummyHandler(reader, writer)


def run_server():
    server = AsyncTcpServer(host="0.0.0.0", port=10400)
    #asyncio.run(server.run(handler_factory=handler_factory))
    asyncio.run(server.run(handler_factory=handler_factory1))


if __name__ == "__main__":
    run_server()
