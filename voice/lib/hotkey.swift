// dictate-hotkey — global hotkeys for dictate and screenrec, no extra apps.
//   🎤/F5  (remapped to F13 by hidutil, see voice/bin/dictate-setup) → `dictate --paste`;
//          pressed again while recording → stop the recording early (SIGUSR1)
//   ⌃⇧5   → `screenrec` (first press starts a screen recording with the mic, second stops it)
// Carbon's RegisterEventHotKey needs no Accessibility or Input Monitoring permission.
import AppKit
import Carbon
import Foundation

// Everything is inside the project: this binary is voice/build/dictate-hotkey, so voice/ is two levels up.
let voice = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
    .deletingLastPathComponent().deletingLastPathComponent()
let dictate = voice.appendingPathComponent("bin/dictate").path
let screenrec = voice.appendingPathComponent("bin/screenrec").path
let pidFile = voice.appendingPathComponent("run/recording.pid").path

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
