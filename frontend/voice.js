// Live voice layer. One interface, two engines:
//   AzureVoiceEngine   - Azure AI Speech (continuous STT + neural TTS) via a short-lived token from /api/speech/token
//   BrowserVoiceEngine - Web Speech API fallback (free, no key; quality varies by browser)
// A future AvatarEngine (Azure TTS avatar / Voice Live) only needs to implement speak()/stopSpeaking()
// with the same callbacks, so the rest of the app does not change.

// Echo protection (speakers feed the assistant's own voice back into the microphone):
//  1. half-duplex by default: recognition results are ignored while speaking and for ECHO_TAIL_MS afterwards
//     (the browser engine stops recognition entirely while speaking)
//  2. echo filter: a transcript that mostly repeats what was just spoken is dropped
//  3. barge-in by voice only when `headphones` is on; otherwise interrupt by tapping the orb / pressing Esc
const BARGE_IN_MIN_CHARS = 4;
const ECHO_TAIL_MS = 800;
const ECHO_WINDOW_MS = 15000;

const words = s => s.toLowerCase().replace(/[^a-z0-9 ]/g, " ").split(/\s+/).filter(w => w.length > 2);

class BaseEngine {
  constructor(cb) {
    this.cb = cb; this.live = false; this.speaking = false; this.queue = [];
    this.headphones = false; this.mutedUntil = 0; this.recent = [];
  }
  _setSpeaking(v) {
    this.speaking = v;
    if (!v) this.mutedUntil = Date.now() + ECHO_TAIL_MS;
    this.cb.onSpeaking?.(v);
  }
  // true while the microphone would mostly hear the assistant itself
  get muted() { return !this.headphones && (this.speaking || Date.now() < this.mutedUntil); }
  isEcho(text) {
    const now = Date.now();
    this.recent = this.recent.filter(r => now - r.t < ECHO_WINDOW_MS);
    const w = words(text);
    if (!w.length) return true;
    return this.recent.some(r => w.filter(x => r.words.has(x)).length / w.length >= 0.6);
  }
  _remember(text) { this.recent.push({ t: Date.now(), words: new Set(words(text)) }); }
  _handleInterim(text) {
    if (this.muted) return;
    this.cb.onInterim?.(text);
    if (this.headphones && this.speaking && text.length >= BARGE_IN_MIN_CHARS && !this.isEcho(text)) {
      this.interrupt(); this.cb.onBargeIn?.();
    }
  }
  _handleFinal(text) {
    if (this.muted || this.isEcho(text)) { this.cb.onInterim?.(""); return; }
    this.cb.onFinal?.(text);
  }
  // queue utterances (acknowledgement, then the answer summary)
  say(text) { if (!text) return; this.queue.push(text); if (!this.speaking) this._next(); }
  _next() { const t = this.queue.shift(); if (t) this._speakNow(t); else this._setSpeaking(false); }
  interrupt() { this.queue = []; this.stopSpeaking(); }
}

export class AzureVoiceEngine extends BaseEngine {
  get name() { return "Azure Speech"; }

  async init(fetchToken) {
    if (!window.SpeechSDK) throw new Error("Speech SDK not loaded");
    this.fetchToken = fetchToken;
    await this._refresh();
    this.timer = setInterval(() => this._refresh().catch(e => this.cb.onError?.(e)), 8 * 60 * 1000);
  }

  async _refresh() {
    const t = await this.fetchToken();
    const SDK = window.SpeechSDK;
    this.voice = t.voice;
    this.cfg = SDK.SpeechConfig.fromAuthorizationToken(t.token, t.region);
    this.cfg.speechRecognitionLanguage = t.language;
    this.cfg.speechSynthesisVoiceName = t.voice;
    // end-of-utterance after 700 ms of silence: snappier turn-taking for conversation
    this.cfg.setProperty(SDK.PropertyId.Speech_SegmentationSilenceTimeoutMs, "700");
    if (this.recognizer) this.recognizer.authorizationToken = t.token;
  }

  async start() {
    const SDK = window.SpeechSDK;
    this.recognizer = new SDK.SpeechRecognizer(this.cfg, SDK.AudioConfig.fromDefaultMicrophoneInput());
    this.recognizer.recognizing = (_, e) => this._handleInterim(e.result.text || "");
    this.recognizer.recognized = (_, e) => {
      if (e.result.reason === SDK.ResultReason.RecognizedSpeech && e.result.text.trim()) this._handleFinal(e.result.text.trim());
    };
    this.recognizer.canceled = (_, e) => { if (e.reason === SDK.CancellationReason.Error) this.cb.onError?.(new Error(e.errorDetails)); };
    await new Promise((ok, fail) => this.recognizer.startContinuousRecognitionAsync(ok, fail));
    this.live = true;
  }

  async stop() {
    this.live = false;
    if (!this.recognizer) return;
    await new Promise(ok => this.recognizer.stopContinuousRecognitionAsync(ok, ok));
    this.recognizer.close(); this.recognizer = null;
  }

  _speakNow(text) {
    const SDK = window.SpeechSDK;
    this._remember(text);
    this._setSpeaking(true);
    this.player = new SDK.SpeakerAudioDestination();
    this.player.onAudioEnd = () => { this._cleanup(); this._next(); };
    this.synth = new SDK.SpeechSynthesizer(this.cfg, SDK.AudioConfig.fromSpeakerOutput(this.player));
    this.synth.speakTextAsync(text, () => { this.synth?.close(); this.synth = null; },
      err => { this.cb.onError?.(new Error(err)); this._cleanup(); this._next(); });
  }

  _cleanup() { try { this.synth?.close(); } catch {} this.synth = null; this.player = null; }

  stopSpeaking() {
    try { this.player?.pause(); this.player?.close(); } catch {}
    this._cleanup(); this._setSpeaking(false);
  }
}

export class BrowserVoiceEngine extends BaseEngine {
  get name() { return "browser speech"; }

  async init() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) throw new Error("This browser has no speech recognition; use Chrome or Edge, or configure Azure Speech");
    this.rec = new SR();
    this.rec.continuous = true; this.rec.interimResults = true; this.rec.lang = "en-US";
    this.rec.onresult = (e) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i], text = r[0].transcript.trim();
        if (r.isFinal) { if (text) this._handleFinal(text); }
        else this._handleInterim(text);
      }
    };
    this.running = false;
    this.rec.onstart = () => { this.running = true; };
    this.rec.onend = () => { this.running = false; this._resume(); };              // keep listening
    this.rec.onerror = (e) => { if (e.error !== "no-speech" && e.error !== "aborted") this.cb.onError?.(new Error(e.error)); };
  }

  // Browser recognition hears the speakers clearly, so it is paused while speaking (unless headphones are on).
  _resume() {
    if (!this.live || this.running) return;
    if (this.muted) { clearTimeout(this.resumeTimer); this.resumeTimer = setTimeout(() => this._resume(), 250); return; }
    try { this.rec.start(); } catch {}
  }
  _pause() { if (!this.headphones && this.running) { try { this.rec.abort(); } catch {} } }

  async start() { this.live = true; this._resume(); }
  async stop() { this.live = false; clearTimeout(this.resumeTimer); try { this.rec.stop(); } catch {} }

  _speakNow(text) {
    this._remember(text);
    this._setSpeaking(true);
    this._pause();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-US"; u.rate = 1.05;
    u.onend = () => { this._next(); this._resume(); };
    u.onerror = () => { this._next(); this._resume(); };
    window.speechSynthesis.speak(u);
  }

  stopSpeaking() { window.speechSynthesis.cancel(); this._setSpeaking(false); this._resume(); }
}

export async function createVoice(config, fetchToken, callbacks) {
  if (config.voice?.provider === "azure") {
    try { const e = new AzureVoiceEngine(callbacks); await e.init(fetchToken); return e; }
    catch (err) { console.warn("Azure Speech unavailable, falling back to browser speech", err); }
  }
  const e = new BrowserVoiceEngine(callbacks); await e.init(); return e;
}

// Short spoken acknowledgement while the agents work ("Checking the scorecard and the initiative charters.")
const ACK = { sql: "the scorecard", charter: "the initiative charters" };
export function acknowledgement(agents) {
  const parts = [...new Set(agents)].map(a => ACK[a]).filter(Boolean);
  if (!parts.length) return "";
  return "Checking " + (parts.length > 1 ? parts.slice(0, -1).join(", ") + " and " + parts.at(-1) : parts[0]) + ".";
}
