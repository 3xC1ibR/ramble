// Ramble.app's executable. It exists so macOS attributes microphone access to
// "Ramble" (TCC prompt + Privacy settings entry). It runs the Python menu bar
// app as a child process, which inherits Ramble's mic permission, and exits
// with it so launchd can restart it.
import Foundation

let repo = (Bundle.main.object(forInfoDictionaryKey: "RambleRepo") as? String) ?? "/path/to/projects/experiments/ramble"
let p = Process()
p.executableURL = URL(fileURLWithPath: repo + "/.venv/bin/python")
p.arguments = ["-m", "ramble.app"]
p.currentDirectoryURL = URL(fileURLWithPath: repo)
var env = ProcessInfo.processInfo.environment
let home = env["HOME"] ?? NSHomeDirectory()
env["PATH"] = "\(home)/.local/bin:\(home)/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
env["PYTHONUNBUFFERED"] = "1"
p.environment = env
do { try p.run() } catch { fputs("ramble: \(error)\n", stderr); exit(1) }
// Forward launchd's SIGTERM/SIGINT to the Python child so it never outlives the app.
var sources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { p.terminate() }
    src.resume()
    sources.append(src)
}
DispatchQueue.global().async { p.waitUntilExit(); exit(p.terminationStatus) }
dispatchMain()
