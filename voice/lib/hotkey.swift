// dictate-hotkey — global hotkeys for dictate and screenrec, no extra apps.
//   🎤/F5  (remapped to F13 by hidutil, see voice/bin/dictate-setup) → `dictate --paste`;
//          pressed again while recording → stop the recording early (SIGUSR1)
//   ⌃⇧5   → `screenrec` (first press starts a screen recording with the mic, second stops it)
// Carbon's RegisterEventHotKey needs no Accessibility or Input Monitoring permission.
//
// Permissions. launchd starts this binary, so macOS (TCC) asks the permissions of THIS binary for
// everything its children do: ffmpeg (Microphone), osascript → System Events (Automation, and
// Accessibility for the paste and Enter). A probe from a terminal checks the terminal app instead,
// so the listener reports its own state: every 5 s it writes voice/run/permissions.json
// (read by voice/bin/doctor --dictation, dictate-setup and dictate-test). The probes are read-only:
// AXIsProcessTrusted, AVCaptureDevice.authorizationStatus, AEDeterminePermissionToAutomateTarget
// with askUserIfNeeded=false. No key is sent, no sound is recorded.
// When voice/run/request-permissions exists (dictate-setup start, dictate-test), the listener asks
// once for the missing ones: the system dialogs appear and the binary is added to each list.
// The binary carries voice/lib/hotkey-Info.plist (NSMicrophoneUsageDescription): without it macOS
// can not show the microphone dialog for a plain binary, and the recording hangs.
import AppKit
import AVFoundation
import Carbon
import Foundation

// Everything is inside the project: this binary is voice/build/dictate-hotkey, so voice/ is two levels up.
let binary = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
let voice = binary.deletingLastPathComponent().deletingLastPathComponent()
let dictate = voice.appendingPathComponent("bin/dictate").path
let screenrec = voice.appendingPathComponent("bin/screenrec").path
let pidFile = voice.appendingPathComponent("run/recording.pid").path
let permFile = voice.appendingPathComponent("run/permissions.json").path
let requestFile = voice.appendingPathComponent("run/request-permissions").path

func run(_ args: [String]) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/zsh")
    p.arguments = args
    try? p.run()
}

func onDictate() {
    if let s = try? String(contentsOfFile: pidFile, encoding: .utf8),
       let pid = Int32(s.trimmingCharacters(in: .whitespacesAndNewlines)), kill(pid, 0) == 0 {
        kill(pid, SIGUSR1)              // second press: stop recording now
        return
    }
    run([dictate, "--paste"])
}

// ---- permissions of this binary (read-only probes) ----
func micState() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "ok"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "not-asked"
    @unknown default: return "unknown"
    }
}

// System Events is the target of the paste (osascript keystroke). -600: it is not running yet.
func automationState(ask: Bool) -> String {
    var addr = AEAddressDesc()
    let bid = "com.apple.systemevents"
    let made = bid.withCString { AECreateDesc(DescType(typeApplicationBundleID), $0, strlen($0), &addr) }
    guard made == noErr else { return "unknown" }
    defer { AEDisposeDesc(&addr) }
    let st = AEDeterminePermissionToAutomateTarget(&addr, AEEventClass(typeWildCard), AEEventID(typeWildCard), ask)
    switch st {
    case noErr: return "ok"
    case OSStatus(errAEEventNotPermitted): return "denied"
    case OSStatus(errAEEventWouldRequireUserConsent): return "not-asked"
    case OSStatus(procNotFound): return "unknown"          // System Events not running: no answer yet
    default: return "error \(st)"
    }
}

var lastWritten = ""
func writeState() {
    let state: [String: Any] = [
        "pid": Int(getpid()),
        "binary": binary.path,
        "microphone": micState(),
        "accessibility": AXIsProcessTrusted() ? "ok" : "denied",
        "automation": automationState(ask: false),
    ]
    guard let data = try? JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]),
          let text = String(data: data, encoding: .utf8) else { return }
    if text == lastWritten && FileManager.default.fileExists(atPath: permFile) { return }
    lastWritten = text
    try? data.write(to: URL(fileURLWithPath: permFile), options: .atomic)
}

// Ask once for what is missing (only on request): the dialogs add this binary to each list.
func askMissing() {
    guard FileManager.default.fileExists(atPath: requestFile) else { return }
    try? FileManager.default.removeItem(atPath: requestFile)
    if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
        AVCaptureDevice.requestAccess(for: .audio) { _ in }
    }
    if !AXIsProcessTrusted() {
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        _ = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
    }
    if automationState(ask: false) != "ok" {
        // start System Events (a faceless system helper) so that macOS can ask about it
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.systemevents") {
            let cfg = NSWorkspace.OpenConfiguration()
            cfg.activates = false
            NSWorkspace.shared.openApplication(at: url, configuration: cfg) { _, _ in
                _ = automationState(ask: true)          // blocks this background queue until the user answers
            }
        }
    }
}

let probeQueue = DispatchQueue(label: "permissions")
let timer = DispatchSource.makeTimerSource(queue: probeQueue)
timer.schedule(deadline: .now(), repeating: 5.0)
timer.setEventHandler { askMissing(); writeState() }
timer.resume()

var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
    var id = EventHotKeyID()
    GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                      nil, MemoryLayout<EventHotKeyID>.size, nil, &id)
    switch id.id {
    case 1: onDictate()
    case 2: run([screenrec])
    default: break
    }
    return noErr
}, 1, &spec, nil, nil)

var refs: [EventHotKeyRef?] = []
func register(_ id: UInt32, _ key: Int, _ mods: Int, _ name: String) {
    var ref: EventHotKeyRef?
    let st = RegisterEventHotKey(UInt32(key), UInt32(mods), EventHotKeyID(signature: OSType(0x44494354), id: id),
                                 GetApplicationEventTarget(), 0, &ref)
    print(st == noErr ? "listening: \(name)" : "could not register \(name): \(st)")
    refs.append(ref)
}
setvbuf(stdout, nil, _IOLBF, 0)
register(1, kVK_F13, 0, "F13 (🎤/F5) → dictate")
register(2, kVK_ANSI_5, controlKey | shiftKey, "⌃⇧5 → screenrec")
NSApplication.shared.setActivationPolicy(.prohibited)
NSApplication.shared.run()
