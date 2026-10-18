# /// script
# requires-python = ">=3.10"
# dependencies = ["azure-communication-callautomation>=1.4", "python-dotenv"]
# ///
"""Ring-only PSTN test: proves ACS can place a call from our US number to a mobile.

The phone rings; when you answer you'll hear silence (no voice yet), and the
script hangs up after HOLD_SECONDS. Set CALLBACK_URL to receive call events;
without it they go nowhere. A later step adds the voice agent.

Usage:  uv run calls/test_call.py +91XXXXXXXXXX
Needs in .env: ACS_CONNECTION_STRING, ACS_PHONE_NUMBER
Optional env CALLBACK_URL: public URL of calls/event_server.py, to see why a call ended
"""
import os
import sys
import time

from azure.communication.callautomation import CallAutomationClient, PhoneNumberIdentifier
from dotenv import load_dotenv

HOLD_SECONDS = 40  # ring time + a few seconds after you answer

load_dotenv()
target = sys.argv[1] if len(sys.argv) > 1 else sys.exit("usage: test_call.py +91XXXXXXXXXX")
client = CallAutomationClient.from_connection_string(os.environ["ACS_CONNECTION_STRING"])

result = client.create_call(
    target_participant=PhoneNumberIdentifier(target),
    source_caller_id_number=PhoneNumberIdentifier(os.environ["ACS_PHONE_NUMBER"]),
    callback_url=os.environ.get("CALLBACK_URL", "https://example.invalid") + "/acs-events",
)
print(f"Call placed to {target}. connection id: {result.call_connection_id}")
print(f"Answer your phone. Hanging up in {HOLD_SECONDS}s...")
time.sleep(HOLD_SECONDS)
try:
    client.get_call_connection(result.call_connection_id).hang_up(is_for_everyone=True)
    print("Hung up.")
except Exception as e:  # already ended (declined, unanswered, or carrier rejected)
    print(f"Call already ended: {e.__class__.__name__}: {e}")
