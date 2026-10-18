// Clinic front-desk softphone: signs in as the "clinic" ACS identity and
// rings when the verification agent calls. Built into agent/static/.
import { CallClient } from "@azure/communication-calling";
import { AzureCommunicationTokenCredential } from "@azure/communication-common";

const $ = (id) => document.getElementById(id);
let callAgent, incoming, call;

function status(text, tone = "") {
  $("status").textContent = text;
  $("status").dataset.tone = tone;
}

function log(line) {
  const li = document.createElement("li");
  li.textContent = `${new Date().toLocaleTimeString()}  ${line}`;
  $("log").prepend(li);
}

async function signIn() {
  $("signin").disabled = true;
  status("Signing in…");
  try {
    const { token } = await (await fetch("/api/token")).json();
    const client = new CallClient();
    const devices = await client.getDeviceManager();
    await devices.askDevicePermission({ audio: true, video: false });
    callAgent = await client.createCallAgent(new AzureCommunicationTokenCredential(token), {
      displayName: "Clinic front desk",
    });
    callAgent.on("incomingCall", ({ incomingCall }) => {
      incoming = incomingCall;
      log("Incoming call from the verification agent");
      status("Incoming call…", "ring");
      $("answer").hidden = false;
      $("decline").hidden = false;
      incomingCall.on("callEnded", () => { if (!call) resetIdle("Missed call"); });
      showAbout();
    });
    $("signin").hidden = true;
    status("Ready — waiting for a call", "ok");
    log("Signed in. Microphone allowed.");
  } catch (e) {
    $("signin").disabled = false;
    status(`Sign-in failed: ${e.message}`, "bad");
  }
}

// Which listing is the agent calling about? (latest call the server placed)
async function showAbout() {
  try {
    const [latest] = await (await fetch("/api/calls")).json();
    if (!latest) return;
    const l = latest.listing;
    $("about-name").textContent = l.full_name;
    $("about-addr").textContent = `${l.address}, ${l.city} ${l.zip} · ${l.phone}`;
    $("about").hidden = false;
    const role = await (await fetch(`/api/role-card/${l.listing_id}`)).json();
    $("role-text").textContent = role.card;
    $("role").hidden = !role.card;
  } catch {}
}

function resetIdle(msg) {
  incoming = call = undefined;
  $("about").hidden = true;
  $("answer").hidden = $("decline").hidden = $("hangup").hidden = true;
  status("Ready — waiting for a call", "ok");
  if (msg) log(msg);
}

async function answer() {
  $("answer").hidden = $("decline").hidden = true;
  call = await incoming.accept({ audioOptions: { muted: false } });
  status("On call with the verification agent", "live");
  $("hangup").hidden = false;
  log("Answered");
  call.on("stateChanged", () => {
    if (call && call.state === "Disconnected") resetIdle("Call ended");
  });
}

$("signin").onclick = signIn;
$("answer").onclick = answer;
$("decline").onclick = () => incoming.reject().then(() => resetIdle("Declined"));
$("hangup").onclick = () => call.hangUp().then(() => resetIdle("Hung up"));
