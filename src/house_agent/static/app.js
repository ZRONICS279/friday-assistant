const form = document.querySelector("#command-form");
const input = document.querySelector("#command");
const transcript = document.querySelector("#transcript");
const tracePanel = document.querySelector("#tool-trace");
const traceToggle = document.querySelector("#trace-toggle");
const traceOutput = document.querySelector("#trace-output");
const stateLabel = document.querySelector("#system-state");
const stateLight = document.querySelector("#state-light");
const hint = document.querySelector("#hint");
const speakerChip = document.querySelector("#speaker-chip");
const speakerName = document.querySelector("#speaker-name");
const suggestions = document.querySelector("#suggestions");

// An empty chat shows the starter chips, and on a phone the home screen in
// place of the transcript. Both follow the same flag.
function setChatIdle(idle) {
  suggestions.hidden = !idle;
  document.body.dataset.chat = idle ? "idle" : "busy";
  Friday.emit("chat-idle", idle);
}
const chatUser = document.querySelector("#chat-user");
const chatUserName = document.querySelector("#chat-user-name");
const convoWho = document.querySelector("#convo-who");
const convoWhoName = document.querySelector("#convo-who-name");

// Tapping the name forgets it: the next typed message comes from nobody in
// particular, until a voice is recognised again.
chatUser.title = "Friday thinks this is you. Tap if it's someone else.";
chatUser.addEventListener("click", () => {
  rememberPerson(null);
  chatUser.hidden = true;
  Friday.toast("Friday won't assume who is typing on this screen now.");
});

// A conversation that sat idle for half an hour is over: picking it back up
// days later drags stale context (old topics, spent pairing windows, spoken
// rules that no longer apply) into every new request — poison for a small
// model. Fresh session, fresh conversation.
const CONVERSATION_IDLE_MS = 30 * 60 * 1000;
let conversationId = localStorage.getItem("house-agent-conversation");
{
  const lastUsed = Number(localStorage.getItem("house-agent-conversation-at") || 0);
  if (conversationId && Date.now() - lastUsed > CONVERSATION_IDLE_MS) {
    conversationId = null;
    localStorage.removeItem("house-agent-conversation");
  }
}

function rememberConversation(id) {
  const changed = id !== conversationId;
  conversationId = id;
  localStorage.setItem("house-agent-conversation", id);
  localStorage.setItem("house-agent-conversation-at", String(Date.now()));
  // Announcements addressed to a conversation only reach the stream that
  // named it, so a new conversation means a new subscription — and its own
  // board of messages from Friday.
  if (changed && typeof connectAnnouncements === "function") connectAnnouncements();
  if (changed && typeof resetInbox === "function") resetInbox();
}

// The conversation id is minted here, before the first recording goes up, so
// speaker identification can start on the very first utterance of a chat.
function ensureConversationId() {
  if (!conversationId) {
    rememberConversation(
      (crypto.randomUUID && crypto.randomUUID()) ||
        `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`,
    );
  }
  return conversationId;
}

const SPEAKER_LABELS = {
  listening: "Listening for voice",
  unknown: "New voice",
  anonymous: "Anonymous guest",
};

// One voice per chat: the chip shows who this chat is locked to.
// Once a name is known — matched by voice, or given in the chat — it sits
// beside "Friday" in the header and above the orb in a voice conversation.
// Until then the chip says what the voice check is doing.
// The person this screen last recognised (by voice, or a name they gave).
const PERSON_KEY = "friday-person";

function screenPerson() {
  try {
    return localStorage.getItem(PERSON_KEY) || null;
  } catch {
    return null;
  }
}

function rememberPerson(name) {
  try {
    if (name) localStorage.setItem(PERSON_KEY, name);
    else localStorage.removeItem(PERSON_KEY);
  } catch {}
}

function paintSpeaker(speaker) {
  let name = speaker && speaker.status !== "disabled"
    ? speaker.name || (speaker.status === "identified" ? speaker.profile : null)
    : null;
  if (name) rememberPerson(name);
  // Before anyone has spoken in this chat, the header shows whose screen it
  // is; the voice screen names only a voice it actually recognised.
  const shown = { header: name || screenPerson(), convo: name };
  for (const [pill, label, who] of [[chatUser, chatUserName, shown.header], [convoWho, convoWhoName, shown.convo]]) {
    pill.hidden = !who;
    label.textContent = who || "";
    pill.querySelector(".user-initial").textContent = who ? who.trim()[0].toUpperCase() : "";
  }
  if (!speaker || speaker.status === "disabled" || name) {
    speakerChip.hidden = true;
    return;
  }
  speakerChip.hidden = false;
  speakerChip.dataset.status = speaker.status;
  speakerName.textContent = SPEAKER_LABELS[speaker.status] || "";
}
let activeAudioUrl;

// Safari and iOS refuse programmatic playback unless the element was started
// inside a user gesture. The spoken reply arrives many seconds after the TALK
// press, so prime one reusable element while the press is still in scope.
const player = new Audio();
let audioUnlocked = false;
const SILENT_WAV =
  "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQAAAAA=";

function unlockAudio() {
  if (audioUnlocked) return;
  audioUnlocked = true;
  player.src = SILENT_WAV;
  const started = player.play();
  if (started) started.catch(() => {});
}

// Spoken replies are on: CONVO is the only way to talk, and it always
// speaks back. The flag stays so announcements know whether to be read aloud.
let soundEnabled = true;

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };

function renderRich(target, message) {
  // The model answers in Markdown. Show it styled instead of showing the
  // raw asterisks; the server strips them separately before synthesis.
  const html = String(message)
    .replace(/[&<>"]/g, (ch) => ESCAPES[ch])
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^[ \t]*#{1,6}[ \t]*/gm, "")
    .replace(/^[ \t]*[-*+][ \t]+/gm, "\u2022 ")
    .replace(/\*\*\*(\S(?:.*?\S)?)\*\*\*/g, "<strong><em>$1</em></strong>")
    .replace(/\*\*(\S(?:.*?\S)?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(\S(?:.*?\S)?)\*/g, "<em>$1</em>")
    .replace(/(^|\s)_(\S(?:.*?\S)?)_(?=\s|$)/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
  target.innerHTML = html;
}

function addMessage(role, message, pending = false) {
  const article = document.createElement("article");
  article.className = `message ${role}${pending ? " pending" : ""}`;
  const label = document.createElement("span");
  label.className = "message-role";
  label.textContent = role === "user" ? "You" : "Friday";
  const text = document.createElement("p");
  renderRich(text, message);
  article.append(label, text);
  // Anything the household says — typed, spoken, or pasted — ends the empty
  // chat, so the phone swaps the home screen for the conversation.
  if (role === "user") setChatIdle(false);
  transcript.append(article);
  transcript.scrollTop = transcript.scrollHeight;
  return article;
}

function setBusy(busy) {
  input.disabled = busy;
  form.querySelector(".send").disabled = busy;
}

function showTrace(trace) {
  if (!trace?.length) return;
  tracePanel.hidden = false;
  traceToggle.querySelector("span").textContent = trace.length;
  traceOutput.textContent = JSON.stringify(trace, null, 2);
}

async function readResponse(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

// Spoken replies arrive as a queue of per-sentence clips so playback can
// start after the first sentence instead of after the whole reply.
const voiceQueue = { clips: [], streamDone: true, active: false, onDrained: null };

function stopSpeech() {
  voiceQueue.clips.length = 0;
  voiceQueue.streamDone = true;
  voiceQueue.active = false;
  voiceQueue.onDrained = null;
  if (!player.paused) player.pause();
  if (activeAudioUrl) {
    URL.revokeObjectURL(activeAudioUrl);
    activeAudioUrl = undefined;
  }
}

function drainVoiceQueue() {
  const drained = voiceQueue.onDrained;
  voiceQueue.onDrained = null;
  drained?.();
}

function blobToDataUrl(clip) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error || new Error("Read failed"));
    reader.readAsDataURL(clip);
  });
}

async function fetchSpeechClip(text, signal) {
  const response = await fetch("/api/speech", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
    signal,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Speech failed (${response.status})`);
  }
  const raw = await response.blob();
  return raw.type ? raw : new Blob([raw], { type: "audio/mpeg" });
}

async function startPlayback(clip) {
  if (sharedAudioContext?.state === "suspended") {
    const resumed = sharedAudioContext.resume();
    if (resumed) resumed.catch(() => {});
  }
  if (activeAudioUrl) URL.revokeObjectURL(activeAudioUrl);
  const audioUrl = URL.createObjectURL(clip);
  activeAudioUrl = audioUrl;
  player.src = audioUrl;
  try {
    await player.play();
  } catch (error) {
    // Safari reports "load failed" for blob: media it will not decode.
    // A data: URL of the same bytes works where the blob URL does not.
    try {
      player.src = await blobToDataUrl(clip);
      await player.play();
    } catch (fallbackError) {
      stopSpeech();
      throw fallbackError;
    }
  }
  return audioUrl;
}

async function playNextClip() {
  const clip = voiceQueue.clips.shift();
  if (!clip) {
    voiceQueue.active = false;
    if (voiceQueue.streamDone) drainVoiceQueue();
    return;
  }
  voiceQueue.active = true;
  try {
    await startPlayback(clip);
    if (convoState === "thinking") setConvoState("speaking", "Speaking — talk to interrupt");
  } catch (error) {
    playNextClip(); // skip a clip the browser will not decode
  }
}

player.addEventListener("ended", () => {
  if (pausedReply) return; // interrupted mid-reply: hold until resume/abandon
  if (voiceQueue.active) playNextClip();
});
player.addEventListener("error", () => {
  if (voiceQueue.active) playNextClip();
});

// Cut streamed text into speakable pieces: every newline, and sentence
// enders once at least ~40 characters have accumulated.
function sentenceFeeder(emit) {
  let buf = "";
  const cut = (end) => {
    const piece = buf.slice(0, end).trim();
    buf = buf.slice(end);
    if (piece) emit(piece);
  };
  return {
    push(text) {
      buf += text;
      let newline;
      while ((newline = buf.indexOf("\n")) >= 0) cut(newline + 1);
      const enders = /[.!?…]["')\]]*\s/g;
      let match;
      while ((match = enders.exec(buf))) {
        const end = match.index + match[0].length;
        if (end >= 40) {
          cut(end);
          break;
        }
      }
    },
    end() {
      cut(buf.length);
    },
    reset() {
      buf = "";
    },
  };
}

// Feed sentences through TTS one at a time (order preserved) into the queue.
function speechFeeder(signal) {
  let chain = Promise.resolve();
  let generation = 0;
  const speakPiece = (sentence) => {
    const mine = generation;
    chain = chain
      .then(() => fetchSpeechClip(sentence, signal))
      .then((clip) => {
        if (signal?.aborted || mine !== generation) return; // discarded meanwhile
        voiceQueue.clips.push(clip);
        if (!voiceQueue.active && !pausedReply) playNextClip();
      })
      .catch(() => {});
  };
  const feeder = sentenceFeeder(speakPiece);
  return {
    push: (text) => feeder.push(text),
    say: speakPiece,
    // The model's text so far was deliberation before a tool call, not the
    // reply: unsay what has not been said. Clips still being synthesized are
    // dropped and queued ones cleared, but a sentence already being spoken
    // FINISHES — cutting audio mid-word sounds broken, and one stray sentence
    // is a smaller cost than a voice that chops itself off.
    discard() {
      generation += 1;
      feeder.reset();
      voiceQueue.clips.length = 0;
      if (convoState === "speaking") setConvoState("thinking", "Thinking");
      // voiceQueue.active stays true while the current clip plays out; its
      // "ended" handler advances to an empty queue and clears it, and any
      // new sentence from the next round queues up behind it seamlessly.
    },
    async end() {
      feeder.end();
      await chain;
      voiceQueue.streamDone = true;
      if (!voiceQueue.active && !voiceQueue.clips.length && !pausedReply) drainVoiceQueue();
    },
  };
}

async function* streamChat(message, signal, mode = "text") {
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // A typed turn says who this screen belongs to, so it gets the same
    // rules, reply style and tastes a recognised voice would.
    body: JSON.stringify({
      message,
      conversation_id: conversationId,
      mode,
      person: mode === "text" ? screenPerson() : null,
    }),
    signal,
  });
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${response.status})`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      // The server heartbeats every 10s even mid-tool, so prolonged silence
      // means the turn is dead (server killed, network dropped). Give up
      // instead of leaving the orb on "Thinking" forever.
      let stallTimer;
      let result;
      try {
        result = await Promise.race([
          reader.read(),
          new Promise((_, reject) => {
            stallTimer = setTimeout(
              () => reject(new Error("Friday stopped responding — please try again.")),
              45000,
            );
          }),
        ]);
      } catch (error) {
        reader.cancel().catch(() => {});
        throw error;
      } finally {
        clearTimeout(stallTimer);
      }
      const { done, value } = result;
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index;
      while ((index = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        // Server-Sent Events: "data: {...}" lines carry events; ":" lines
        // are comments/padding; blank lines separate records.
        if (line.startsWith("data:")) yield JSON.parse(line.slice(5).trim());
      }
    }
  } finally {
    reader.releaseLock();
  }
}

// A spoken "on it" the moment the model commits to a slow tool, so the
// silence before a long lookup never feels dead. Quick device commands
// (roku_press, power, inputs) get no ack — they just happen.
const ACK_POOLS = [
  {
    match: /^(content_recommend|catalog_search|watch_history_find)$/,
    lines: [
      "Let me take a look.",
      "Checking.",
      "One sec.",
      "Let me see what's out there.",
      "Looking now.",
      "Give me a moment.",
    ],
  },
  {
    match: /^(content_play|content_plan_playback)$/,
    lines: ["On it.", "Getting that ready.", "One moment.", "Sure thing."],
  },
];

// Said while a planning request's reasoning runs, which can take a while.
const THINKING_LINES = ["Let me think about that.", "Good question, one moment.", "Let me work that out.", "Hmm, give me a second."];

function toolAck(name) {
  for (const pool of ACK_POOLS) {
    if (pool.match.test(name)) {
      return pool.lines[Math.floor(Math.random() * pool.lines.length)];
    }
  }
  return null;
}

// One streamed agent turn: text renders into pendingEl token by token, and
// (when speak is on) each finished sentence is synthesized and queued while
// the model is still writing the next one.
async function runStreamedTurn({ message, pendingEl, speak, signal, onTool }) {
  let feeder = null;
  if (speak) {
    voiceQueue.clips.length = 0;
    voiceQueue.streamDone = false;
    voiceQueue.active = false;
    feeder = speechFeeder(signal);
  }
  const paragraph = pendingEl.querySelector("p");
  let text = "";
  let result;
  let acked = false;
  for await (const event of streamChat(message, signal, speak ? "voice" : "text")) {
    if (event.type === "token") {
      text += event.text;
      renderRich(paragraph, text);
      transcript.scrollTop = transcript.scrollHeight;
      feeder?.push(event.text);
    } else if (event.type === "reset") {
      text = "";
      paragraph.textContent = "Working…";
      feeder?.discard();
    } else if (event.type === "meta") {
      rememberConversation(event.conversation_id);
    } else if (event.type === "thinking") {
      if (!text) paragraph.textContent = "Thinking…";
      if (feeder && !acked && !text) {
        feeder.say(THINKING_LINES[Math.floor(Math.random() * THINKING_LINES.length)]);
        acked = true;
      }
    } else if (event.type === "tool") {
      if (feeder && !acked && !text) {
        const ack = toolAck(event.name);
        if (ack) {
          feeder.say(ack);
          acked = true;
        }
      }
      onTool?.(event.name);
    } else if (event.type === "done") {
      result = event;
    } else if (event.type === "error") {
      throw new Error(event.detail);
    }
  }
  if (!result) throw new Error("The reply stream ended unexpectedly.");
  rememberConversation(result.conversation_id);
  Friday.emit("turn", result.conversation_id);
  if (result.speaker) paintSpeaker(result.speaker);
  renderRich(paragraph, result.message);
  pendingEl.classList.remove("pending");
  showTrace(result.trace);
  if (feeder) {
    if (!text) feeder.push(result.message); // e.g. the bare "Done." fallback
    await feeder.end();
  }
  return result;
}

async function sendText(message) {
  setChatIdle(false);
  addMessage("user", message);
  const pending = addMessage("assistant", "Working…", true);
  setBusy(true);
  try {
    await runStreamedTurn({ message, pendingEl: pending, speak: false });
  } catch (error) {
    pending.querySelector("p").textContent = error.message;
    pending.classList.remove("pending");
  } finally {
    setBusy(false);
    input.focus();
  }
}

// The page a phone lands on after signing in to DoorDash lives on the house
// computer (localhost:4180), so it cannot load there; its address, pasted
// here, carries the code that finishes the sign-in. Hand it straight to the
// server — no need for the model to work out what it is.
const DOORDASH_CALLBACK = /localhost:4180\/oauth2\/callback\?\S*code=/i;

async function finishDoorDashSignIn(pasted) {
  addMessage("user", "Pasted the DoorDash sign-in address.");
  const reply = addMessage("assistant", "Finishing the DoorDash sign-in…", true);
  try {
    const data = await readResponse(
      await fetch("/api/doordash/callback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: ensureConversationId(), callback: pasted }),
      }),
    );
    reply.classList.remove("pending");
    renderRich(reply.querySelector("p"), data.text || (data.ok ? "DoorDash is signed in." : data.error));
  } catch (error) {
    reply.classList.remove("pending");
    reply.querySelector("p").textContent = `Could not finish the sign-in: ${error.message}`;
  }
  transcript.scrollTop = transcript.scrollHeight;
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  if (DOORDASH_CALLBACK.test(message)) {
    finishDoorDashSignIn(message);
    return;
  }
  sendText(message);
});

// Starter prompts under an empty chat; a tap sends one as typed.
suggestions.addEventListener("click", (event) => {
  const chip = event.target.closest(".suggestion");
  if (!chip || input.disabled) return;
  input.value = chip.textContent;
  form.requestSubmit();
});

traceToggle.addEventListener("click", () => {
  const expanded = traceToggle.getAttribute("aria-expanded") === "true";
  traceToggle.setAttribute("aria-expanded", String(!expanded));
  traceOutput.hidden = expanded;
});

document.querySelector("#new-chat").addEventListener("click", () => {
  // A fresh conversation id: past replies stop steering the model's style,
  // and the context the backend replays each turn shrinks back down.
  conversationId = null;
  localStorage.removeItem("house-agent-conversation");
  paintSpeaker(null);
  stopSpeech();
  transcript.replaceChildren();
  addMessage("assistant", "Fresh start. What do you need?");
  tracePanel.hidden = true;
  hint.textContent = privacyHint();
  setChatIdle(true);
  // Mint the new id now: the announcement stream and the messages board
  // switch to the new chat immediately, not at the first message.
  ensureConversationId();
  setInboxOpen(false);
  Friday.emit("conversation", conversationId);
});

// Reopen a past chat (from the history drawer, or the one this screen was
// on before a reload): its messages replace the transcript and new turns
// continue it.
async function openConversation(id, { unlessBusy = false } = {}) {
  const data = await Friday.api(`/api/conversations/${encodeURIComponent(id)}`);
  // On reload, a message typed while this was loading wins over the restore.
  if (unlessBusy && transcript.querySelector(".message.user")) return;
  stopSpeech();
  rememberConversation(id);
  transcript.replaceChildren();
  let lastTrace = null;
  for (const message of data.messages || []) {
    if (message.role !== "user" && message.role !== "assistant") continue;
    addMessage(message.role, message.content);
    if (message.trace?.length) lastTrace = message.trace;
  }
  if (!transcript.children.length) addMessage("assistant", "Fresh start. What do you need?");
  tracePanel.hidden = true;
  showTrace(lastTrace);
  setChatIdle(transcript.querySelector(".message.user") === null);
  paintSpeaker(data.speaker || null);
  Friday.emit("conversation", conversationId);
}

if (conversationId) {
  openConversation(conversationId, { unlessBusy: true }).catch(() => {});
  // Returning to a chat: show who it is locked to, if anyone.
  fetch(`/api/voice/speaker?conversation_id=${encodeURIComponent(conversationId)}`)
    .then(readResponse)
    .then((body) => paintSpeaker(body.speaker))
    .catch(() => {});
}

// The backend's state shows in the sidebar, the chat header and the More sheet.
let cloudLabel = null; // the cloud provider answering, or null when local

function privacyHint() {
  return cloudLabel
    ? `Replies come from ${cloudLabel} in the cloud; what you say is sent there.`
    : "Everything stays on this machine.";
}

function paintStatus(text, kind) {
  cloudLabel = kind === "cloud" ? text.split(" via ")[1]?.replace(" (cloud)", "") || "a cloud AI" : null;
  if (hint.textContent === "Everything stays on this machine." || hint.textContent.startsWith("Replies come from")) {
    hint.textContent = privacyHint();
  }
  stateLabel.textContent = text;
  stateLight.className = `state-light ${kind}`;
  // The sidebar promises privacy only while the model is local.
  document.querySelector(".rail-foot-title").textContent = kind === "cloud" ? "Cloud AI in use" : "Local & private";
  document.querySelector(".rail-foot").title =
    kind === "cloud"
      ? "Conversations are sent to the cloud AI chosen in Settings."
      : "Runs on this house. Nothing leaves the LAN.";
  document.querySelector("#chat-status").textContent = kind === "ok" || kind === "cloud" ? `Online · ${text}` : text;
  document.querySelector("#chat-light").className = `state-light ${kind}`;
  document.querySelector("#more-status").textContent = kind === "ok" || kind === "cloud" ? `Online · ${text}` : text;
  document.querySelector("#more-light").className = `state-light ${kind}`;
}

Friday.on("assistant-model", (data) => {
  paintStatus(
    data.cloud ? `${data.model} via ${data.label} (cloud)` : data.model,
    data.cloud ? "cloud" : "ok",
  );
});

fetch("/api/health")
  .then(readResponse)
  .then((body) => {
    if (!body.paired) {
      window.FridayAuth.askForPairingCode();
      return;
    }
    const tvs = Number(body.tv_devices) || 0;
    paintStatus(
      body.llm_cloud ? `${body.model} via ${body.llm_label} (cloud)` : `${body.model} · ${tvs} TV${tvs === 1 ? "" : "s"}`,
      body.llm_cloud ? "cloud" : "ok",
    );
    document.body.dataset.online = "true";
    if (body.first_run && !conversationId) {
      // Nobody enrolled yet: the very first exchange is the introduction.
      transcript.replaceChildren();
      addMessage(
        "assistant",
        "Hi — I'm brand new here and don't know anyone's voice yet. Tap Talk, " +
          "say hello and your name, and I'll remember you. Then we can " +
          "set up what you like to watch, or skip that for now.",
      );
      hint.textContent = "First-time setup: introduce yourself by voice.";
    }
  })
  .catch(() => {
    paintStatus("Backend offline", "bad");
    document.body.dataset.online = "false";
  });

// ---------------------------------------------------------------------------
// Conversation mode: hands-free back-and-forth behind a full-screen orb.
// A level meter watches the open microphone; talking starts a turn, ~1s of
// silence ends it. The mic stays open while the reply plays, so speaking over
// it pauses playback — short acknowledgements ("okay", "mhmm") resume the
// reply, anything else abandons it and becomes the next command.
// ---------------------------------------------------------------------------
const convoOverlay = document.querySelector("#convo");
const convoOrb = document.querySelector("#convo-orb");
const convoStatus = document.querySelector("#convo-status");
const convoExit = document.querySelector("#convo-exit");
const convoButton = document.querySelector("#convo-button");
const convoMute = document.querySelector("#convo-mute");

const SPEECH_HOLD_MS = 250; // voice must persist this long to count as speech
const SILENCE_END_MS = 900; // this much quiet ends the utterance
const MIN_UTTERANCE_MS = 450; // anything shorter is a bump, not a word
const RECORDER_RECYCLE_MS = 15000; // cap how much leading silence a blob holds

let convoState = "idle"; // idle | listening | capturing | thinking | speaking
let convoStream;
let convoAnalyser;
let convoLevelData;
let convoFreqData;
let convoTicker;
let micSource;
let sharedAudioContext;
let outputAnalyser;
let outputData;
let sceneFrame;

// iOS Safari cannot tap an <audio> element's output without breaking it, so
// the reply visual there runs on a synthesized cadence instead of a real FFT.
const IS_IOS =
  /iP(hone|ad|od)/.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

function ensureAudioGraph() {
  if (!sharedAudioContext) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    sharedAudioContext = new AudioContextClass();
    if (!IS_IOS) {
      try {
        const routed = sharedAudioContext.createMediaElementSource(player);
        outputAnalyser = sharedAudioContext.createAnalyser();
        outputAnalyser.fftSize = 256;
        outputAnalyser.smoothingTimeConstant = 0.8;
        routed.connect(outputAnalyser);
        outputAnalyser.connect(sharedAudioContext.destination);
        outputData = new Uint8Array(outputAnalyser.frequencyBinCount);
      } catch (error) {
        outputAnalyser = undefined;
      }
    }
  }
  if (sharedAudioContext.state === "suspended") {
    const resumed = sharedAudioContext.resume();
    if (resumed) resumed.catch(() => {});
  }
  return sharedAudioContext;
}
let noiseFloor = 0.004;
let calibrateUntil = 0;
let voiceSince = 0;
let quietSince = 0;
let captureStartedAt = 0;
let pausedReply = false; // a spoken reply is paused awaiting resume/abandon
let utterance; // the currently running utterance recorder
let turnToken = 0;
let inflight; // AbortController for the turn being processed
let convoTurnBusy = false; // a turn's tools/generation are still running

function setConvoState(state, label) {
  convoState = state;
  convoOverlay.dataset.state = state;
  convoStatus.textContent = label;
}

function startUtterance() {
  const chunks = [];
  const recorder = new MediaRecorder(convoStream);
  recorder.addEventListener("dataavailable", (event) => chunks.push(event.data));
  utterance = { recorder, chunks, startedAt: Date.now() };
  recorder.start();
  return utterance;
}

function stopUtterance(handle, discard = false) {
  return new Promise((resolve) => {
    const finish = () => {
      resolve(
        discard
          ? undefined
          : new Blob(handle.chunks, { type: handle.recorder.mimeType || "audio/webm" }),
      );
    };
    if (handle.recorder.state === "inactive") {
      finish();
      return;
    }
    handle.recorder.addEventListener("stop", finish, { once: true });
    handle.recorder.stop();
  });
}

function recycleUtterance() {
  if (utterance) stopUtterance(utterance, true);
  startUtterance();
}

function micLevel() {
  convoAnalyser.getByteTimeDomainData(convoLevelData);
  let sum = 0;
  for (let i = 0; i < convoLevelData.length; i += 1) {
    const v = (convoLevelData[i] - 128) / 128;
    sum += v * v;
  }
  return Math.sqrt(sum / convoLevelData.length);
}

const BACKCHANNEL_WORDS = new Set([
  "ok", "okay", "kay", "yeah", "yea", "yes", "yep", "yup", "ya",
  "mhm", "mhmm", "mm", "mmm", "hmm", "uh", "huh", "uhhuh",
  "right", "sure", "cool", "nice", "good", "great", "alright", "awesome",
  "perfect", "thanks", "thank", "you", "please", "haha", "lol",
  "got", "it", "i", "see", "go", "on", "keep", "going", "continue", "sounds",
]);

// Speech that clearly redirects the assistant away from what it is doing.
// Anything else heard while tools are running is treated as side talk and
// the task in flight is allowed to finish.
const REDIRECT_CUES =
  /\b(no|stop|wait|actually|cancel|nevermind|never mind|instead|don'?t|forget (it|that)|hold on|not that)\b/i;

function isBackchannel(text) {
  const words = text.toLowerCase().replace(/[^a-z\s]/g, " ").split(/\s+/).filter(Boolean);
  return words.length > 0 && words.length <= 4 && words.every((w) => BACKCHANNEL_WORDS.has(w));
}

function enterListening() {
  if (convoState === "idle") return;
  pausedReply = false;
  voiceSince = 0;
  quietSince = 0;
  recycleUtterance();
  setConvoState("listening", "Listening");
}

function resumeReply() {
  pausedReply = false;
  recycleUtterance();
  setConvoState("speaking", "Speaking — talk to interrupt");
  if (!player.ended && player.src && player.paused && voiceQueue.active) {
    const resumed = player.play();
    if (resumed) resumed.catch(() => playNextClip());
  } else {
    playNextClip();
  }
}

function beginCapture() {
  voiceSince = 0;
  quietSince = 0;
  captureStartedAt = Date.now();
  if (convoState === "speaking") {
    pausedReply = true;
    player.pause();
  }
  setConvoState("capturing", "");
}

async function endCapture() {
  const utteranceMs = (quietSince || Date.now()) - captureStartedAt + SPEECH_HOLD_MS;
  const wasInterrupt = pausedReply;
  setConvoState("thinking", "Thinking");
  const blob = await stopUtterance(utterance);
  if (convoState === "idle") return;
  // Keep capturing right away so speech during the model's turn is not lost:
  // a real utterance while it thinks supersedes the turn in flight.
  startUtterance();

  const resumeOrListen = () => {
    if (convoState === "idle") return;
    if (wasInterrupt && (voiceQueue.active || voiceQueue.clips.length)) resumeReply();
    else if (convoState === "thinking") enterListening();
  };

  if (utteranceMs < MIN_UTTERANCE_MS || !blob || blob.size < 1000) {
    resumeOrListen();
    return;
  }

  let text = "";
  try {
    const data = new FormData();
    data.append("audio", blob, "command.webm");
    data.append("conversation_id", ensureConversationId());
    const heard = await readResponse(
      await fetch("/api/transcribe", { method: "POST", body: data }),
    );
    paintSpeaker(heard.speaker);
    text = (heard.transcript || "").trim();
  } catch (error) {
    resumeOrListen();
    return;
  }
  if (convoState === "idle") return;
  if (!text) {
    resumeOrListen();
    return;
  }
  if (wasInterrupt && isBackchannel(text)) {
    resumeReply();
    return;
  }
  if (convoTurnBusy && !wasInterrupt) {
    // Tools are running. Only an explicit redirect overrides the work in
    // flight; acknowledgements and side conversation let it finish.
    if (isBackchannel(text) || !REDIRECT_CUES.test(text)) {
      setConvoState("thinking", "Finishing what I started");
      return;
    }
    // The user said stop/no/actually — kill the turn server-side too, so
    // its tools stop instead of completing in the background.
    if (conversationId) {
      fetch("/api/chat/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: conversationId }),
      }).catch(() => {});
    }
  }

  // A real command: drop any paused reply for good, cancel the turn in
  // flight, and start this one.
  turnToken += 1;
  const myTurn = turnToken;
  inflight?.abort();
  inflight = new AbortController();
  const { signal } = inflight;
  stopSpeech();
  pausedReply = false;
  setConvoState("thinking", "Thinking");

  addMessage("user", text);
  const pending = addMessage("assistant", "Working…", true);
  try {
    voiceQueue.onDrained = () => {
      // "thinking" too: a reply with nothing speakable (or whose clips all
      // failed) must still hand the mic back instead of hanging the orb.
      if (
        (convoState === "speaking" || convoState === "thinking") &&
        myTurn === turnToken
      ) {
        enterListening();
      }
    };
    recycleUtterance();
    convoTurnBusy = true;
    try {
      await runStreamedTurn({
        message: text,
        pendingEl: pending,
        speak: true,
        signal,
        onTool: (name) => {
          if (myTurn === turnToken && convoState === "thinking") {
            setConvoState("thinking", name.replaceAll("_", " "));
          }
        },
      });
    } finally {
      if (myTurn === turnToken) convoTurnBusy = false;
    }
    // Playback continues past this point; the drained hook returns us to
    // listening once the last queued sentence has been spoken.
  } catch (error) {
    pending.classList.remove("pending");
    if (error.name === "AbortError" || convoState === "idle" || myTurn !== turnToken) return;
    pending.querySelector("p").textContent = error.message;
    setConvoState("listening", error.message);
    setTimeout(() => {
      if (convoState === "listening") setConvoState("listening", "Listening");
    }, 4000);
    recycleUtterance();
  }
}

// Mute: the mic stays open but is ignored, so the user can have a side
// conversation without starting a capture or interrupting a reply.
let convoMuted = false;

function setConvoMuted(muted) {
  convoMuted = muted;
  convoMute.setAttribute("aria-pressed", muted ? "true" : "false");
  convoMute.textContent = muted ? "Mic muted · tap to unmute" : "Mute mic";
  voiceSince = 0;
  quietSince = 0;
  if (muted && convoState === "capturing") {
    // Muting right after speaking means "I'm done — go work on that":
    // finish the utterance and process it, exactly as silence would have.
    endCapture();
  }
  if (!muted && utterance) {
    recycleUtterance(); // the side conversation is not part of any command
  }
}

convoMute.addEventListener("click", () => setConvoMuted(!convoMuted));

let thinkingIdleSince = 0;

function convoTick() {
  if (!convoAnalyser || convoState === "idle") return;
  const level = micLevel();
  const now = Date.now();

  // Watchdog: "thinking" with no turn running and no audio queued or playing
  // is a dead end (a turn that ended silently, or speech judged side-talk
  // after the reply already finished). Give it a grace period for the gap
  // between transcription and the turn starting, then hand the mic back.
  if (
    convoState === "thinking" &&
    !convoTurnBusy &&
    !voiceQueue.active &&
    !voiceQueue.clips.length
  ) {
    if (!thinkingIdleSince) thinkingIdleSince = now;
    else if (now - thinkingIdleSince > 8000) {
      thinkingIdleSince = 0;
      enterListening();
      return;
    }
  } else {
    thinkingIdleSince = 0;
  }

  // Muted: ignore the microphone entirely (no captures, no interrupts).
  // The watchdog above still runs so turn state cannot strand while muted.
  if (convoMuted) return;

  if (now < calibrateUntil) {
    // Follow the level down instantly but up only gently, so words, a TV,
    // or the mic's auto-gain settling during calibration cannot poison the
    // floor and leave the whole session demanding a raised voice.
    noiseFloor = level < noiseFloor ? level : noiseFloor * 0.9 + level * 0.1;
    return;
  }
  noiseFloor = Math.min(noiseFloor, 0.05);
  let threshold = Math.min(Math.max(noiseFloor * 2.5, 0.012), 0.13);
  // Echo cancellation keeps most of the reply out of the mic, but not all of
  // it on loudspeakers — demand a firmer voice to count as an interruption.
  if (convoState === "speaking") threshold *= 1.8;
  // Hysteresis: starting speech takes a firm voice, but once capturing,
  // quieter trailing speech still counts — lowering your voice at the end
  // of a sentence is not the end of the utterance.
  if (convoState === "capturing") threshold *= 0.55;
  const voiced = level > threshold;

  if (convoState === "capturing") {
    if (voiced) {
      quietSince = 0;
    } else {
      if (!quietSince) quietSince = now;
      if (now - quietSince >= SILENCE_END_MS) endCapture();
    }
    return;
  }

  // listening / speaking / thinking: watch for speech starting. Cutting the
  // reply off is costly, so a sound must last longer than a bump to do it.
  const holdMs = convoState === "speaking" ? SPEECH_HOLD_MS * 1.6 : SPEECH_HOLD_MS;
  if (voiced) {
    if (!voiceSince) voiceSince = now;
    if (now - voiceSince >= holdMs) beginCapture();
  } else {
    voiceSince = 0;
    // Track the room's noise so the threshold follows slow changes — but
    // never learn from sound loud enough to be quiet speech: feeding soft
    // talking into the floor ratchets the threshold up until only yelling
    // registers. Suspiciously loud "silence" instead drifts the floor down.
    if (level < threshold * 0.5) noiseFloor = noiseFloor * 0.995 + level * 0.005;
    else noiseFloor *= 0.999;
    if (
      (convoState === "listening" || convoState === "speaking") &&
      utterance &&
      now - utterance.startedAt > RECORDER_RECYCLE_MS
    ) {
      recycleUtterance();
    }
  }
}

async function enterConvo() {
  if (convoState !== "idle") return;
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    hint.textContent = window.isSecureContext
      ? "This browser does not support microphone recording."
      : "Microphone needs HTTPS. Open this page over https:// or on localhost.";
    return;
  }
  soundEnabled = true;
  unlockAudio();
  stopSpeech();
  try {
    convoStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (error) {
    hint.textContent = `Microphone unavailable: ${error.message}`;
    return;
  }
  const audioContext = ensureAudioGraph();
  micSource = audioContext.createMediaStreamSource(convoStream);
  convoAnalyser = audioContext.createAnalyser();
  convoAnalyser.fftSize = 1024;
  convoAnalyser.smoothingTimeConstant = 0.75;
  micSource.connect(convoAnalyser);
  convoLevelData = new Uint8Array(convoAnalyser.fftSize);
  convoFreqData = new Uint8Array(convoAnalyser.frequencyBinCount);

  convoOverlay.hidden = false;
  startConvoScene();
  noiseFloor = 0.004;
  calibrateUntil = Date.now() + 900;
  pausedReply = false;
  voiceSince = 0;
  quietSince = 0;
  setConvoState("listening", "Listening");
  setConvoMuted(false);
  startUtterance();
  convoTicker = setInterval(convoTick, 50);
}

function exitConvo() {
  turnToken += 1;
  inflight?.abort();
  clearInterval(convoTicker);
  setConvoState("idle", "");
  if (utterance) stopUtterance(utterance, true);
  utterance = undefined;
  convoStream?.getTracks().forEach((track) => track.stop());
  convoStream = undefined;
  micSource?.disconnect();
  micSource = undefined;
  convoAnalyser = undefined;
  cancelAnimationFrame(sceneFrame);
  stopSpeech();
  pausedReply = false;
  convoOverlay.hidden = true;
  hint.textContent = privacyHint();
}

convoButton.addEventListener("click", enterConvo);
// The round button in the phone tab bar: talk from any page.
document.querySelector("#nav-talk").addEventListener("click", () => {
  Friday.navigate("chat");
  enterConvo();
});
convoExit.addEventListener("click", exitConvo);

// ---------------------------------------------------------------------------
// The reactor: a canvas ring of spectrum bars around a glowing core.
// Capturing — bars follow YOUR voice (mic FFT). Speaking — bars follow the
// assistant's voice (output FFT; synthesized cadence on iOS). Listening —
// a faint ring that stirs with room sound. Thinking — orbiting arcs.
// ---------------------------------------------------------------------------
const BAR_COUNT = 72;
const pseudoData = new Uint8Array(BAR_COUNT);
let pseudoPhase = 0;
let smoothLevel = 0;

const STATE_HUES = {
  listening: { bar: "138, 149, 232", core: "160, 168, 240", gain: 0.35 },
  capturing: { bar: "110, 196, 142", core: "150, 220, 170", gain: 1.0 },
  thinking: { bar: "128, 87, 213", core: "128, 87, 213", gain: 0 },
  speaking: { bar: "196, 186, 255", core: "224, 226, 255", gain: 0.9 },
};

function pseudoSpeechBins() {
  // No output tap available: fake a speech-like cadence while audio plays.
  pseudoPhase += 0.22;
  const talking = voiceQueue.active && !player.paused && !pausedReply;
  const envelope = talking
    ? 0.45 + 0.55 * Math.abs(Math.sin(pseudoPhase * 0.55)) * (0.6 + 0.4 * Math.sin(pseudoPhase * 0.13))
    : 0;
  for (let i = 0; i < BAR_COUNT; i += 1) {
    const wave =
      0.5 +
      0.3 * Math.sin(pseudoPhase + i * 0.55) +
      0.2 * Math.sin(pseudoPhase * 1.7 + i * 1.3);
    pseudoData[i] = Math.max(0, Math.min(255, wave * envelope * 255));
  }
  return pseudoData;
}

function sceneBins() {
  if (convoState === "capturing" && convoAnalyser) {
    convoAnalyser.getByteFrequencyData(convoFreqData);
    return convoFreqData;
  }
  if (convoState === "speaking") {
    if (outputAnalyser) {
      outputAnalyser.getByteFrequencyData(outputData);
      return outputData;
    }
    return pseudoSpeechBins();
  }
  if (convoState === "listening" && convoAnalyser) {
    convoAnalyser.getByteFrequencyData(convoFreqData);
    return convoFreqData;
  }
  return null;
}

function renderConvoScene() {
  sceneFrame = requestAnimationFrame(renderConvoScene);
  const cw = convoOrb.clientWidth;
  if (!cw || convoState === "idle") return;
  const dpr = window.devicePixelRatio || 1;
  if (convoOrb.width !== Math.round(cw * dpr)) {
    convoOrb.width = Math.round(cw * dpr);
    convoOrb.height = Math.round(cw * dpr);
  }
  const g = convoOrb.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, cw, cw);

  const c = cw / 2;
  const R = cw * 0.3;
  const t = performance.now() / 1000;
  const hues = STATE_HUES[convoState] || STATE_HUES.listening;
  const bins = sceneBins();

  // Average level drives the core; smoothed so it swells instead of jittering.
  let level = 0;
  if (bins) {
    let sum = 0;
    const used = Math.floor(bins.length * 0.6) || bins.length;
    for (let i = 0; i < used; i += 1) sum += bins[i];
    level = (sum / used / 255) * hues.gain;
  }
  smoothLevel += (level - smoothLevel) * 0.25;

  // Spectrum ring: symmetric bars, slowly revolving.
  if (bins && hues.gain > 0) {
    const spin = t * 0.18;
    const half = BAR_COUNT / 2;
    g.lineWidth = Math.max(2, cw * 0.008);
    g.lineCap = "round";
    for (let i = 0; i < BAR_COUNT; i += 1) {
      const mirrored = i < half ? i : BAR_COUNT - 1 - i;
      const bin = Math.floor((mirrored / half) * bins.length * 0.55);
      const value = (bins[bin] || 0) / 255;
      const length = cw * 0.012 + value * hues.gain * cw * 0.14;
      const angle = (i / BAR_COUNT) * Math.PI * 2 + spin;
      const inner = R + cw * 0.035;
      g.strokeStyle = `rgba(${hues.bar}, ${0.25 + value * 0.65})`;
      g.beginPath();
      g.moveTo(c + Math.cos(angle) * inner, c + Math.sin(angle) * inner);
      g.lineTo(c + Math.cos(angle) * (inner + length), c + Math.sin(angle) * (inner + length));
      g.stroke();
    }
  }

  // Thinking: three arcs orbiting at different speeds.
  if (convoState === "thinking") {
    g.lineWidth = Math.max(2, cw * 0.009);
    g.lineCap = "round";
    const orbit = R + cw * 0.07;
    const arcs = [
      { speed: 1.6, offset: 0, span: 1.3, alpha: 0.85 },
      { speed: -1.1, offset: 2.1, span: 0.9, alpha: 0.5 },
      { speed: 0.7, offset: 4.2, span: 0.6, alpha: 0.3 },
    ];
    for (const arc of arcs) {
      const start = t * arc.speed + arc.offset;
      g.strokeStyle = `rgba(${hues.bar}, ${arc.alpha})`;
      g.beginPath();
      g.arc(c, c, orbit, start, start + arc.span);
      g.stroke();
    }
  }

  // Core: breathing glow that swells with the sound.
  const breathe = 1 + 0.02 * Math.sin(t * 1.4);
  const coreR = (R * 0.82 + smoothLevel * R * 0.5) * breathe;
  const glow = g.createRadialGradient(c, c, coreR * 0.1, c, c, coreR * 1.9);
  glow.addColorStop(0, `rgba(${hues.core}, ${0.5 + smoothLevel * 0.4})`);
  glow.addColorStop(0.55, `rgba(${hues.core}, 0.14)`);
  glow.addColorStop(1, `rgba(${hues.core}, 0)`);
  g.fillStyle = glow;
  g.beginPath();
  g.arc(c, c, coreR * 1.9, 0, Math.PI * 2);
  g.fill();

  const core = g.createRadialGradient(c - coreR * 0.3, c - coreR * 0.3, coreR * 0.1, c, c, coreR);
  core.addColorStop(0, "rgba(255, 255, 255, 0.95)");
  core.addColorStop(0.6, `rgba(${hues.core}, 0.85)`);
  core.addColorStop(1, `rgba(${hues.core}, 0.35)`);
  g.fillStyle = core;
  g.beginPath();
  g.arc(c, c, coreR, 0, Math.PI * 2);
  g.fill();

  // Boundary ring.
  g.lineWidth = 1;
  g.strokeStyle = `rgba(${hues.bar}, 0.28)`;
  g.beginPath();
  g.arc(c, c, R + cw * 0.02, 0, Math.PI * 2);
  g.stroke();
}

function startConvoScene() {
  cancelAnimationFrame(sceneFrame);
  sceneFrame = requestAnimationFrame(renderConvoScene);
}

// ---------------------------------------------------------------------------
// Announcements: reminders, scheduled checks, and anything else Friday says
// without being asked. The server pushes them over Server-Sent Events; the
// browser reconnects on its own and replays what it missed by event id.
let lastAnnouncementId = Number(localStorage.getItem("house-agent-announced") || 0);

function speakAnnouncement(text, attempt = 0) {
  if (!soundEnabled && convoState === "idle") return;
  // Mid-turn in conversation mode: wait for the reply to finish rather than
  // splicing a reminder into the middle of it.
  if ((convoState === "capturing" || convoState === "thinking") && attempt < 10) {
    setTimeout(() => speakAnnouncement(text, attempt + 1), 3000);
    return;
  }
  if (convoState === "listening") {
    setConvoState("speaking", "Speaking — talk to interrupt");
    voiceQueue.onDrained = () => {
      if (convoState === "speaking") enterListening();
    };
  }
  voiceQueue.streamDone = false;
  const feeder = speechFeeder();
  feeder.push(text);
  feeder.end();
}

function showAnnouncement(data) {
  if (data.id <= lastAnnouncementId) return;
  lastAnnouncementId = data.id;
  localStorage.setItem("house-agent-announced", String(lastAnnouncementId));
  const article = addMessage("assistant", data.text);
  article.classList.add("announcement");
  article.querySelector(".message-role").textContent = "Reminder";
  speakAnnouncement(data.text);
}

let announcementSource = null;

function connectAnnouncements() {
  if (!("EventSource" in window)) return;
  // Resolve the id first: minting one re-enters here, and the stream that
  // call opens must be the one closed below, not left dangling.
  const conversation = ensureConversationId();
  if (announcementSource) announcementSource.close();
  // Household-wide events (reminders) reach every screen; events addressed
  // to this conversation (the light that just joined ITS pairing window)
  // reach only this one — other people talking to the house never see them.
  const params = window.FridayAuth.withToken(new URLSearchParams({ since: String(lastAnnouncementId), conversation }));
  const source = new EventSource(`/api/events?${params}`);
  announcementSource = source;
  source.addEventListener("announce", (event) => {
    try {
      showAnnouncement(JSON.parse(event.data));
    } catch (error) {
      console.warn("Bad announcement", error);
    }
  });
  source.addEventListener("inbox", (event) => {
    try {
      onInboxEvent(JSON.parse(event.data));
    } catch (error) {
      console.warn("Bad inbox event", error);
    }
  });
  // Errors are transient (server restart, sleep/wake); EventSource retries.
  source.onerror = () => {};
}

// ---------------------------------------------------------------------------
// Messages from Friday: links, sign-in pages, and instructions she puts on the
// screen instead of reading aloud. One-way. Stored server-side so a phone
// opened later still sees them; the badge counts what nobody has opened yet.
const inboxPanel = document.querySelector("#inbox");
const inboxToggle = document.querySelector("#inbox-toggle");
const inboxBadge = document.querySelector("#inbox-badge");
const inboxList = document.querySelector("#inbox-list");
const inboxEmpty = document.querySelector("#inbox-empty");
const inboxClose = document.querySelector("#inbox-close");
const inboxSeen = new Set();
let inboxUnread = 0;

function paintInboxBadge(count) {
  inboxUnread = Math.max(0, Number(count) || 0);
  inboxBadge.hidden = inboxUnread === 0;
  inboxBadge.textContent = inboxUnread > 99 ? "99+" : String(inboxUnread);
  inboxToggle.classList.toggle("has-unread", inboxUnread > 0);
  inboxToggle.setAttribute(
    "aria-label",
    inboxUnread ? `Messages from Friday, ${inboxUnread} new` : "Messages from Friday",
  );
}

function inboxTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const today = new Date().toDateString() === date.toDateString();
  return date.toLocaleString([], today
    ? { hour: "numeric", minute: "2-digit" }
    : { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function renderInboxItem(message) {
  const item = document.createElement("li");
  item.className = `inbox-item${message.read ? "" : " unread"}`;
  item.dataset.id = String(message.id);
  const head = document.createElement("div");
  head.className = "inbox-item-head";
  const title = document.createElement("span");
  title.className = "inbox-title";
  title.textContent = message.title;
  const when = document.createElement("span");
  when.className = "inbox-when";
  when.textContent = inboxTime(message.created_at);
  head.append(title, when);
  item.append(head);
  if (message.body) {
    const body = document.createElement("p");
    body.className = "inbox-body";
    body.textContent = message.body;
    item.append(body);
  }
  if (message.url) {
    const actions = document.createElement("div");
    actions.className = "inbox-actions";
    const open = document.createElement("a");
    open.className = "open";
    open.href = message.url;
    open.target = "_blank";
    open.rel = "noopener";
    open.textContent = "Open link";
    const copy = document.createElement("button");
    copy.type = "button";
    copy.textContent = "Copy";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(new URL(message.url, location.origin).href);
        copy.textContent = "Copied";
      } catch {
        copy.textContent = "Select below";
      }
      setTimeout(() => { copy.textContent = "Copy"; }, 1800);
    });
    actions.append(open, copy);
    const url = document.createElement("p");
    url.className = "inbox-url";
    url.textContent = new URL(message.url, location.origin).href;
    item.append(actions, url);
  }
  return item;
}

function showInboxMessages(messages, { prepend = false } = {}) {
  for (const message of messages) {
    if (!message || inboxSeen.has(message.id)) continue;
    inboxSeen.add(message.id);
    const item = renderInboxItem(message);
    if (prepend) inboxList.prepend(item);
    else inboxList.append(item);
  }
  inboxEmpty.hidden = inboxList.children.length > 0;
}

async function loadInbox() {
  try {
    const params = new URLSearchParams({ conversation: ensureConversationId(), limit: "50" });
    const data = await readResponse(await fetch(`/api/inbox?${params}`));
    showInboxMessages(data.messages || []);
    paintInboxBadge(data.unread);
    // Opened before the list arrived: what is showing now has been seen.
    if (!inboxPanel.hidden) markInboxRead();
  } catch (error) {
    console.warn("Messages unavailable", error);
  }
}

async function markInboxRead() {
  if (inboxUnread === 0) return;
  try {
    const data = await readResponse(
      await fetch("/api/inbox/read", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: ensureConversationId() }),
      }),
    );
    paintInboxBadge(data.unread);
  } catch (error) {
    console.warn("Could not mark messages read", error);
  }
}

// The envelope floats just above the command bar; the bar's height depends on
// the viewport (two rows on a phone), so measure rather than guess. On the
// convo screen CSS moves it to the corner instead.
function placeInbox() {
  if (document.body.dataset.view !== "chat") {
    // Off the chat page the bar is not on screen: sit in the corner, above
    // the tab bar on a phone (CSS adds that offset).
    document.documentElement.style.setProperty("--inbox-bottom", "18px");
    return;
  }
  const rect = form.getBoundingClientRect();
  const bottom = Math.max(18, Math.round(window.innerHeight - rect.top + 12));
  document.documentElement.style.setProperty("--inbox-bottom", `${bottom}px`);
}
Friday.on("view", () => placeInbox());

function setInboxOpen(open) {
  inboxPanel.hidden = !open;
  inboxToggle.setAttribute("aria-expanded", String(open));
  if (open) {
    placeInbox();
    markInboxRead();
  } else {
    // The highlight stays while the panel is open so new arrivals stand out;
    // it clears once they have been looked at.
    inboxList.querySelectorAll(".unread").forEach((item) => item.classList.remove("unread"));
  }
}

// A new chat is a new board: nothing from the last conversation carries over.
function resetInbox() {
  inboxSeen.clear();
  inboxList.replaceChildren();
  inboxEmpty.hidden = false;
  paintInboxBadge(0);
  loadInbox();
}

function onInboxEvent(data) {
  if (data.id > lastAnnouncementId) {
    lastAnnouncementId = data.id;
    localStorage.setItem("house-agent-announced", String(lastAnnouncementId));
  }
  if (!data.message) return;
  if (data.updated) {
    // Same card, renewed contents (a sign-in link minted afresh): swap it in
    // place, keeping whether it had been looked at.
    const existing = inboxList.querySelector(`[data-id="${data.message.id}"]`);
    if (existing) {
      const fresh = renderInboxItem(data.message);
      fresh.classList.toggle("unread", existing.classList.contains("unread"));
      existing.replaceWith(fresh);
      return;
    }
  }
  showInboxMessages([data.message], { prepend: true });
  if (inboxPanel.hidden) {
    paintInboxBadge(typeof data.unread === "number" ? data.unread : inboxUnread + 1);
  } else {
    inboxUnread += 1;
    markInboxRead();
  }
}

inboxToggle.addEventListener("click", () => setInboxOpen(inboxPanel.hidden));
inboxClose.addEventListener("click", () => setInboxOpen(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !inboxPanel.hidden) setInboxOpen(false);
});
window.addEventListener("resize", placeInbox);
window.addEventListener("scroll", placeInbox, { passive: true });
new ResizeObserver(placeInbox).observe(document.querySelector(".console"));
placeInbox();
loadInbox();

connectAnnouncements();
