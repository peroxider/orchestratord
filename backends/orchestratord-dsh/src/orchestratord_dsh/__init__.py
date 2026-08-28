"""orchestratord-dsh — DeepSeek Harness SdkProcess backend.

Wraps ``deepseek-harness-sdk`` (synchronous) via ``asyncio.to_thread``
into the orchestratord SPI.  This is the reference SdkProcess
implementation and the contract test comparison target.
"""

from orchestratord_dsh.backend import DshBackend

__all__ = ["DshBackend"]
