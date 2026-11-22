// GeniusConsole — the console (console/index.html, served by console/server.py) in its own macOS window.
//   a menu-bar app only (accessory: no Dock icon, not in ⌘Tab); it starts HIDDEN and never takes
//          the focus by itself: the window comes up only on ⌥Space or the menu-bar item's «Show»
//   one window. «Fill screen» (the default): on every show it takes exactly the visible frame of the
//          screen under the pointer (below the menu bar, above the Dock): borderless, square, opaque,
//          no shadow, so nothing of the windows behind shows at its edges. A manual resize, or
//          «Fill screen» unchecked, makes it a normal window (transparent title bar over the page; the
//          traffic lights appear while the pointer is in the top-left corner; drag it by its top 8 px).
//          The choice and the manual frame are kept in console/run/window.json
//   a menu-bar item (crosshair): Show/Hide · Skin: Hunter / Classic · Reload · Quit
//   ⌥Space (global, Carbon RegisterEventHotKey: no Accessibility or Input Monitoring permission)
//          shows and focuses the window, or hides it when it is already in front
//   --root <genius-ai folder>: the data the bridge reads (default: the folder that holds this app)
//   the bridge: if nothing answers on 127.0.0.1:4419 the app starts, headless (no terminal window;
//          output to console/run/bridge.log),
//          `python3 -I console/server.py --root <root> --port 4419` and stops it on quit; a bridge that
//          already runs is reused and left running; a bridge started by a crashed earlier instance
//          (console/run/app.json) is adopted and stopped on quit
// Nothing global (AGENTS.md «Voice»): no NSUserDefaults, no window restoration, a non-persistent
// WebKit data store; the page's localStorage is mirrored into console/run/storage.json.
// No permission is asked: F5 dictation is the voice listener; it pastes with ⌘V into the focused line.
// Build: console/bin/console build  (swiftc only, into console/build/GeniusConsole)
import AppKit
import Carbon
import Foundation
import WebKit

// This binary is console/build/GeniusConsole: console/ is two levels up, genius-ai three.
let binary = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
let consoleDir = binary.deletingLastPathComponent().deletingLastPathComponent()
let root: URL = {
    let a = CommandLine.arguments
    if let i = a.firstIndex(of: "--root"), i + 1 < a.count {
        return URL(fileURLWithPath: (a[i + 1] as NSString).expandingTildeInPath).standardizedFileURL
    }
    return consoleDir.deletingLastPathComponent()
}()
let runDir = consoleDir.appendingPathComponent("run")
let port: UInt16 = UInt16(ProcessInfo.processInfo.environment["GENIUS_CONSOLE_PORT"] ?? "") ?? 4419   // env: tests only
let pageURL = URL(string: "http://127.0.0.1:\(port)/")!

func log(_ s: String) {
    let line = "\(ISO8601DateFormatter().string(from: Date())) \(s)\n"
    FileHandle.standardError.write(line.data(using: .utf8)!)
}

func readJSON(_ name: String) -> [String: Any] {
    guard let d = try? Data(contentsOf: runDir.appendingPathComponent(name)),
          let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return [:] }
    return o
}

func writeJSON(_ name: String, _ o: Any) {
    guard let d = try? JSONSerialization.data(withJSONObject: o, options: [.sortedKeys]) else { return }
    try? d.write(to: runDir.appendingPathComponent(name), options: .atomic)
}

// Does anything accept a TCP connection on 127.0.0.1:port?
func portAnswers() -> Bool {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    if fd < 0 { return false }
    defer { close(fd) }
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = port.bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    let r = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
    }
    return r == 0
}

// ---- the bridge ----
func commandOf(_ pid: pid_t) -> String {
    let p = Process(), pipe = Pipe()
    p.executableURL = URL(fileURLWithPath: "/bin/ps")
    p.arguments = ["-p", String(pid), "-o", "command="]
    p.standardOutput = pipe; p.standardError = FileHandle.nullDevice
    guard (try? p.run()) != nil else { return "" }
    p.waitUntilExit()
    return String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
}

final class Bridge {
    var child: Process?
    var adopted: pid_t = 0                           // started by a crashed earlier instance
    var owned: Bool { child != nil || adopted != 0 }
    var pid: pid_t { child?.processIdentifier ?? adopted }

    // A bridge that an earlier instance started and did not stop (it crashed): ours to stop.
    func adoptOrphan() -> Bool {
        let s = readJSON("app.json")
        guard s["bridge_owned"] as? Bool == true, let bp = s["bridge_pid"] as? Int, bp > 0,
              let ap = s["pid"] as? Int, ap != Int(getpid()) else { return false }
        if kill(pid_t(ap), 0) == 0 && commandOf(pid_t(ap)).contains("GeniusConsole") { return false }  // still alive
        let cmd = commandOf(pid_t(bp))
        guard cmd.contains("console/server.py"), cmd.contains("--port \(port)") else { return false }
        adopted = pid_t(bp)
        log("bridge: adopted pid \(bp), left by a crashed instance")
        return true
    }

    // Reuse a running bridge, or start ours; `ready` runs on the main queue when it answers.
    func ensure(_ ready: @escaping (Bool) -> Void) {
        if portAnswers() {
            if !adoptOrphan() { log("bridge: reusing the one on :\(port)") }
            ready(true); return
        }
        let server = consoleDir.appendingPathComponent("server.py").path
        let p = Process()
        // a login shell gives the bridge the user's PATH (gh, hc, herdr-app) when the app is not
        // started from a terminal; `exec` makes the python process the child we stop on quit
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-lc", "exec python3 -I \"$0\" --root \"$1\" --port \"$2\"", server, root.path, String(port)]
        p.currentDirectoryURL = root
        let logURL = runDir.appendingPathComponent("bridge.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        if let h = try? FileHandle(forWritingTo: logURL) { p.standardOutput = h; p.standardError = h }
        p.standardInput = FileHandle.nullDevice
        do { try p.run() } catch { log("bridge: cannot start: \(error)"); ready(false); return }
        child = p
        log("bridge: started pid \(p.processIdentifier) (log console/run/bridge.log)")
        var tries = 0
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { t in
            tries += 1
            if portAnswers() { t.invalidate(); ready(true) }
            else if !p.isRunning || tries > 80 { t.invalidate(); log("bridge: did not answer"); ready(false) }
        }
    }

    func stop() {
        if adopted != 0 {
            let a = adopted
            kill(a, SIGTERM)
            let until = Date().addingTimeInterval(2)
            while kill(a, 0) == 0 && Date() < until { usleep(50_000) }
            if kill(a, 0) == 0 { kill(a, SIGKILL) }
            log("bridge: stopped adopted pid \(a)")
            adopted = 0
        }
        guard let p = child, p.isRunning else { return }
        p.terminate()
        let until = Date().addingTimeInterval(2)
        while p.isRunning && Date() < until { usleep(50_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        log("bridge: stopped pid \(p.processIdentifier)")
        child = nil
    }
}

// ---- the page's localStorage, kept in console/run/storage.json (the WebKit store is in memory) ----
func storageScript() -> WKUserScript {
    let saved = (try? String(contentsOf: runDir.appendingPathComponent("storage.json"), encoding: .utf8)) ?? "{}"
    let js = """
    (function () {
      try { if (localStorage.length === 0) { const s = \(saved); for (const k in s) localStorage.setItem(k, String(s[k])); } } catch (e) {}
      const P = Storage.prototype, set = P.setItem, rem = P.removeItem, clr = P.clear;
      const post = (st) => { if (st !== localStorage) return; try { const o = {};
        for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); o[k] = localStorage.getItem(k); }
        window.webkit.messageHandlers.geniusStore.postMessage(JSON.stringify(o)); } catch (e) {} };
      P.setItem = function (k, v) { set.call(this, k, v); post(this); };
      P.removeItem = function (k) { rem.call(this, k); post(this); };
      P.clear = function () { clr.call(this); post(this); };
    })();
    """
    return WKUserScript(source: js, injectionTime: .atDocumentStart, forMainFrameOnly: true)
}

// Network: the bridge only, plus the pinned three.js on jsdelivr (a fallback when the page does not
// find console/vendor/three.module.js on an older bridge). Everything else is blocked.
let rules = """
[{"trigger": {"url-filter": ".*"}, "action": {"type": "block"}},
 {"trigger": {"url-filter": "^http://127\\\\.0\\\\.0\\\\.1:\(port)/"}, "action": {"type": "ignore-previous-rules"}},
 {"trigger": {"url-filter": "^http://localhost:\(port)/"}, "action": {"type": "ignore-previous-rules"}},
 {"trigger": {"url-filter": "^https://cdn\\\\.jsdelivr\\\\.net/npm/three@0\\\\.170\\\\.0/"}, "action": {"type": "ignore-previous-rules"}}]
"""

// ---- the window ----
// Hover only: clicks pass through to the page below.
final class HoverZone: NSView {
    var onHover: ((Bool) -> Void)?
    override func hitTest(_ p: NSPoint) -> NSView? { nil }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self))
    }
    override func mouseEntered(with e: NSEvent) { onHover?(true) }
    override func mouseExited(with e: NSEvent) { onHover?(false) }
}

final class DragStrip: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
    override func mouseDown(with e: NSEvent) {
        guard window?.styleMask.contains(.titled) == true else { return }    // «Fill screen»: no moving
        if e.clickCount == 2 { window?.zoom(nil) } else { window?.performDrag(with: e) }
    }
}

final class ConsoleWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

final class App: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandler, NSMenuDelegate {
    let bridge = Bridge()
    var window: ConsoleWindow!
    var web: WKWebView!
    var status: NSStatusItem!
    var hotKey: EventHotKeyRef?
    var hotKeyState = "not registered"
    var loaded = false
    var signals: [DispatchSourceSignal] = []
    var titlebar: NSView?
    var fill = (readJSON("window.json")["fill"] as? Bool) ?? true
    var manualFrame = NSRect.zero

    func applicationDidFinishLaunching(_ n: Notification) {
        try? FileManager.default.createDirectory(at: runDir, withIntermediateDirectories: true)
        buildMenu()
        buildWindow()
        buildStatusItem()
        registerHotKey()
        for s in [SIGTERM, SIGINT, SIGHUP] {          // console/bin/console stop → a clean quit
            signal(s, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: s, queue: .main)
            src.setEventHandler { NSApp.terminate(nil) }
            src.resume()
            signals.append(src)
        }
        WKContentRuleListStore(url: runDir.appendingPathComponent("webkit-rules"))?
            .compileContentRuleList(forIdentifier: "genius-console", encodedContentRuleList: rules) { list, err in
                if let list = list { self.web.configuration.userContentController.add(list) }
                else { log("rules: \(String(describing: err))") }
                self.bridge.ensure { ok in
                    self.writeState()
                    if ok { self.load() } else { self.offline() }
                }
            }
        // no show() here: the app starts hidden and never activates itself (only ⌥Space or «Show»)
        if ProcessInfo.processInfo.environment["GENIUS_CONSOLE_TEST"] == "1" { startTestChannel() }
    }

    // ---- self-test channel (only with GENIUS_CONSOLE_TEST=1; console/bin/console selftest) ----
    // A distributed notification «genius.console.test» with {cmd, arg, out}: the commands go through
    // the app's own code paths (toggle = the hotkey's action; keys = NSApp.sendEvent, like the keyboard).
    func startTestChannel() {
        log("test channel on")
        DistributedNotificationCenter.default().addObserver(forName: Notification.Name("genius.console.test"), object: nil, queue: .main) { n in
            guard let i = n.userInfo, let cmd = i["cmd"] as? String else { return }
            let arg = i["arg"] as? String ?? "", out = i["out"] as? String ?? ""
            let reply = { (s: String) in try? s.write(toFile: out, atomically: true, encoding: .utf8) }
            switch cmd {
            case "toggle": self.toggle(); DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                reply("visible=\(self.window.isVisible) active=\(NSApp.isActive) key=\(self.window.isKeyWindow)") }
            case "state": reply("visible=\(self.window.isVisible) active=\(NSApp.isActive) key=\(self.window.isKeyWindow)")
            case "snapshot":
                self.web.takeSnapshot(with: nil) { img, err in
                    guard let img = img, let t = img.tiffRepresentation, let r = NSBitmapImageRep(data: t),
                          let png = r.representation(using: .png, properties: [:]) else { reply("error \(String(describing: err))"); return }
                    try? png.write(to: URL(fileURLWithPath: arg)); reply("ok")
                }
            case "eval":
                self.web.evaluateJavaScript(arg) { r, e in reply(e.map { "error \($0)" } ?? "\(r ?? "nil")") }
            case "keys":                                   // «text» or «cmd+v»
                let cmdDown = arg.hasPrefix("cmd+")
                let chars = cmdDown ? String(arg.dropFirst(4)) : arg
                for ch in chars {
                    let c = String(ch)
                    let code: UInt16 = c == "\r" ? 36 : c == "\u{1b}" ? 53 : c == "v" ? 9 : c == "k" ? 40 : c == "`" ? 50 : 0
                    for type in [NSEvent.EventType.keyDown, .keyUp] {
                        if let e = NSEvent.keyEvent(with: type, location: .zero, modifierFlags: cmdDown ? .command : [],
                                                    timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: self.window.windowNumber,
                                                    context: nil, characters: c, charactersIgnoringModifiers: c, isARepeat: false, keyCode: code) {
                            NSApp.sendEvent(e)
                        }
                    }
                }
                reply("sent")
            default: reply("unknown")
            }
        }
    }

    func applicationWillTerminate(_ n: Notification) {
        saveFrame()
        bridge.stop()
        try? FileManager.default.removeItem(at: runDir.appendingPathComponent("app.json"))
    }


    // ---- menus ----
    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Hide Console", action: #selector(hideWindow), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Console", action: #selector(quit), keyEquivalent: "q")
        appItem.submenu = appMenu
        // Edit: without these key equivalents a WKWebView gets no ⌘V (F5 dictation pastes with ⌘V)
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r")
        let fs = view.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        fs.keyEquivalentModifierMask = [.command, .control]
        view.addItem(withTitle: "Close Window", action: #selector(hideWindow), keyEquivalent: "w")
        viewItem.submenu = view
        NSApp.mainMenu = main
    }

    func buildStatusItem() {
        status = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let b = status.button {
            let img = NSImage(systemSymbolName: "scope", accessibilityDescription: "genius-ai console")
            img?.isTemplate = true
            b.image = img
            b.toolTip = "genius-ai console · ⌥Space"
        }
        let m = NSMenu()
        m.delegate = self
        status.menu = m
    }

    func menuNeedsUpdate(_ m: NSMenu) {
        m.removeAllItems()
        let visible = window.isVisible && NSApp.isActive
        m.addItem(withTitle: visible ? "Hide  (⌥Space)" : "Show  (⌥Space)", action: #selector(toggle), keyEquivalent: "").target = self
        m.addItem(.separator())
        let skin = currentSkin
        for (title, name) in [("Skin: Hunter", "hunter"), ("Skin: Classic", "classic")] {
            let i = m.addItem(withTitle: title, action: #selector(setSkin(_:)), keyEquivalent: "")
            i.target = self; i.representedObject = name; i.state = skin == name ? .on : .off
        }
        m.addItem(.separator())
        let f = m.addItem(withTitle: "Fill screen", action: #selector(toggleFill), keyEquivalent: "")
        f.target = self; f.state = fill ? .on : .off
        m.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "").target = self
        let b = m.addItem(withTitle: bridge.owned ? "Bridge: started by the app" : "Bridge: reused (left running on quit)", action: nil, keyEquivalent: "")
        b.isEnabled = false
        if hotKey == nil { m.addItem(withTitle: "⌥Space: \(hotKeyState)", action: nil, keyEquivalent: "").isEnabled = false }
        m.addItem(.separator())
        m.addItem(withTitle: "Quit", action: #selector(quit), keyEquivalent: "").target = self
    }

    var currentSkin = ""

    @objc func setSkin(_ i: NSMenuItem) {
        guard let name = i.representedObject as? String else { return }
        // the page's own switch (the same as K): click the skin's button
        web.evaluateJavaScript("document.querySelector('#skin button[data-skin=\"\(name)\"]')?.click(); document.body.dataset.skin") { r, _ in
            if let s = r as? String { self.currentSkin = s }
        }
        currentSkin = name
    }

    func readSkin() {
        web.evaluateJavaScript("document.body.dataset.skin") { r, _ in if let s = r as? String { self.currentSkin = s } }
    }

    // ---- the window and the page ----
    func buildWindow() {
        manualFrame = savedFrame() ?? defaultFrame()
        let frame = fill ? fillFrame() : manualFrame
        window = ConsoleWindow(contentRect: frame, styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                               backing: .buffered, defer: false)
        window.title = "genius-ai console"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.backgroundColor = .black
        window.isOpaque = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.isRestorable = false
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 640, height: 420)
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenPrimary]
        window.delegate = self
        window.setFrame(frame, display: false)

        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .nonPersistent()          // nothing in ~/Library/WebKit
        cfg.userContentController.addUserScript(storageScript())
        cfg.userContentController.add(self, name: "geniusStore")
        cfg.preferences.javaScriptCanOpenWindowsAutomatically = false
        cfg.suppressesIncrementalRendering = false
        web = WKWebView(frame: window.contentView!.bounds, configuration: cfg)
        web.autoresizingMask = [.width, .height]
        web.setValue(false, forKey: "drawsBackground")   // black window behind the page while it loads
        web.underPageBackgroundColor = .black
        web.allowsBackForwardNavigationGestures = false
        web.allowsMagnification = false
        web.navigationDelegate = self
        window.contentView!.addSubview(web)

        let b = window.contentView!.bounds
        let corner = HoverZone(frame: NSRect(x: 0, y: b.height - 36, width: 96, height: 36))
        corner.autoresizingMask = [.minYMargin, .maxXMargin]
        corner.onHover = { [weak self] inside in self?.titlebar?.isHidden = !inside }
        window.contentView!.addSubview(corner)
        // the top 8 px (above the page's own top labels) drag the window; a double click zooms it
        let strip = DragStrip(frame: NSRect(x: 96, y: b.height - 8, width: b.width - 96, height: 8))
        strip.autoresizingMask = [.width, .minYMargin]
        window.contentView!.addSubview(strip)
        applyStyle()
        window.setFrame(frame, display: false)
    }

    // «Fill screen»: borderless (square corners, no shadow). Otherwise a normal window whose title bar
    // (with the traffic lights) is hidden, so it does not eat the page's clicks in the top 28 px.
    func applyStyle() {
        if fill {
            window.styleMask = [.borderless, .resizable]
            window.hasShadow = false
            titlebar = nil
        } else {
            window.styleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
            window.titleVisibility = .hidden
            window.titlebarAppearsTransparent = true
            window.hasShadow = true
            titlebar = window.standardWindowButton(.closeButton)?.superview?.superview
            titlebar?.isHidden = true
        }
        window.isOpaque = true
        window.backgroundColor = .black
    }

    // the visible frame of the screen under the pointer: below the menu bar, above the Dock
    func fillFrame() -> NSRect {
        let at = NSEvent.mouseLocation
        guard let screen = NSScreen.screens.first(where: { NSMouseInRect(at, $0.frame, false) }) ?? NSScreen.main
        else { return defaultFrame() }
        // below the menu bar, down to the bottom edge: Herdr reaches the bottom edge, so stopping above
        // the Dock's reserved strip left Herdr showing under the console
        let f = screen.frame
        return NSRect(x: f.minX, y: f.minY, width: f.width, height: screen.visibleFrame.maxY - f.minY)
    }

    @objc func toggleFill() {
        fill.toggle()
        applyStyle()
        window.setFrame(fill ? fillFrame() : manualFrame, display: true)
        saveFrame()
        if window.isVisible { window.makeFirstResponder(web) }
    }

    func defaultFrame() -> NSRect {
        let v = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let w = min(1440, v.width - 80), h = min(900, v.height - 80)
        return NSRect(x: v.midX - w / 2, y: v.midY - h / 2, width: w, height: h)
    }

    func savedFrame() -> NSRect? {
        guard let s = readJSON("window.json")["frame"] as? String else { return nil }
        let r = NSRectFromString(s)
        guard r.width >= 640, r.height >= 420,
              NSScreen.screens.contains(where: { $0.visibleFrame.intersects(r) }) else { return nil }
        return r
    }

    func saveFrame() {
        guard let w = window, !w.styleMask.contains(.fullScreen) else { return }
        if !fill { manualFrame = w.frame }
        writeJSON("window.json", ["frame": NSStringFromRect(manualFrame), "fill": fill])
    }

    func windowDidMove(_ n: Notification) { if !fill { saveFrame() } }
    // a resize by the owner (a live resize; never our own setFrame) ends «Fill screen»
    func windowDidEndLiveResize(_ n: Notification) {
        if fill {
            let f = window.frame
            fill = false
            applyStyle()
            window.setFrame(f, display: true)
            window.makeFirstResponder(web)
        }
        saveFrame()
    }
    func windowShouldClose(_ s: NSWindow) -> Bool { hideWindow(); return false }   // close = hide; Quit quits

    func writeState() {
        writeJSON("app.json", ["pid": Int(getpid()), "window": window.windowNumber, "bridge_owned": bridge.owned,
                               "bridge_pid": Int(bridge.pid), "hotkey": hotKeyState,
                               "port": Int(port), "root": root.path])
    }

    func load() { web.load(URLRequest(url: pageURL)) }

    func offline() {
        let html = """
        <html><body style="background:#000;color:#ff6a50;font:13px ui-monospace,Menlo,monospace;padding:40px">
        BRIDGE OFFLINE · nothing answers on 127.0.0.1:\(port) and the app could not start console/server.py.<br>
        See console/run/bridge.log. View → Reload (⌘R) tries again.</body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    @objc func reload() {
        if portAnswers() { if loaded { web.reload() } else { load() } }
        else { bridge.ensure { ok in self.writeState(); ok ? self.load() : self.offline() } }
    }

    // only the bridge's own pages; anything else stays out of the window
    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let u = a.request.url
        let ok = u?.scheme == "about" || (u?.host == "127.0.0.1" && u?.port == Int(port)) || (u?.host == "localhost" && u?.port == Int(port))
        decisionHandler(ok ? .allow : .cancel)
    }

    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        loaded = w.url?.host == "127.0.0.1"
        readSkin()
        focusPage()
    }

    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        log("load failed: \(e.localizedDescription)")
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { if !self.loaded { self.reload() } }
    }

    func webViewWebContentProcessDidTerminate(_ w: WKWebView) { log("web content process ended; reloading"); load() }

    func userContentController(_ u: WKUserContentController, didReceive m: WKScriptMessage) {
        guard m.name == "geniusStore", let s = m.body as? String else { return }
        try? s.write(to: runDir.appendingPathComponent("storage.json"), atomically: true, encoding: .utf8)
        if let d = s.data(using: .utf8), let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
           let skin = o["genius.console.skin"] as? String { currentSkin = skin }
    }

    // ---- show / hide ----
    func focusPage() {
        guard window.isKeyWindow else { return }
        window.makeFirstResponder(web)
    }

    @objc func show() {
        log("show")
        if fill { window.setFrame(fillFrame(), display: true) }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(web)
    }

    @objc func hideWindow() {
        log("hide")
        window.orderOut(nil)
        NSApp.hide(nil)                                  // the previous app gets the keyboard back
    }

    @objc func toggle() {
        if window.isVisible && NSApp.isActive && window.isKeyWindow { hideWindow() } else { show() }
    }

    @objc func quit() { NSApp.terminate(nil) }

    // ---- ⌥Space ----
    func registerHotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, ctx in
            let me = Unmanaged<App>.fromOpaque(ctx!).takeUnretainedValue()
            DispatchQueue.main.async { me.toggle() }
            return noErr
        }, 1, &spec, Unmanaged.passUnretained(self).toOpaque(), nil)
        let st = RegisterEventHotKey(UInt32(kVK_Space), UInt32(optionKey),
                                     EventHotKeyID(signature: OSType(0x47434F4E), id: 1),   // 'GCON'
                                     GetApplicationEventTarget(), 0, &hotKey)
        if st == noErr { hotKeyState = "⌥Space" }
        else { hotKey = nil; hotKeyState = "taken by another app (error \(st))" }
        log("hotkey: \(hotKeyState)")
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)                  // menu bar only: no Dock icon, not in ⌘Tab
app.run()
