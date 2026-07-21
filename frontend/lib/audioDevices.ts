// Shared by VideoRecorder.tsx and VoicePanel.tsx — both call getUserMedia
// for a microphone, and both hit the same real bug: unconstrained
// `audio: true` lets the browser pick its own default device, which on at
// least one real system resolved to "Stereo Mix" (a loopback device that
// captures system OUTPUT audio, not the microphone) instead of the actual
// mic — even though the browser's own reported "Default" alias pointed at
// the right device. The recorded track still looks perfectly healthy
// (live, unmuted) since Stereo Mix genuinely is a live device; it just
// captures near-silence during a normal recording with nothing playing
// through the speakers.

// Loopback/monitor device name patterns across common vendors — "Stereo
// Mix" (Realtek/most onboard audio) being the one confirmed live, with
// known equivalents from other vendors/platforms included defensively.
const LOOPBACK_DEVICE_PATTERNS = [
  /stereo mix/i,
  /loopback/i,
  /what u hear/i,
  /wave out mix/i,
  /cable output/i,
  /monitor of/i,
]

export function isLikelyLoopbackDevice(label: string): boolean {
  return LOOPBACK_DEVICE_PATTERNS.some((p) => p.test(label))
}

// Prefer whichever real (non-loopback) input the browser itself labels
// "Default" (Chrome's own alias for the OS-designated default recording
// device) over just taking the first non-loopback entry — that's the
// signal that was actually correct on the system this bug was found on.
export function pickPreferredAudioDevice(
  devices: MediaDeviceInfo[]
): MediaDeviceInfo | undefined {
  const realInputs = devices.filter(
    (d) => d.kind === 'audioinput' && d.label && !isLikelyLoopbackDevice(d.label)
  )
  return realInputs.find((d) => d.label.startsWith('Default')) || realInputs[0]
}
