"""Allow running as: python -m agent"""
from agent.cli import main
import asyncio

asyncio.run(main())
