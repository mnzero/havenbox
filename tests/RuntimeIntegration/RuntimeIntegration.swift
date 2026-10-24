import Foundation
import HavenboxCore

@main
struct RuntimeIntegration {
    private static func stage(_ value: String) {
        FileHandle.standardError.write(Data("stage=\(value)\n".utf8))
    }

    static func main() throws {
        let values = Array(CommandLine.arguments.dropFirst())
        guard values.count == 14 || values.count == 15 else {
            throw RuntimeError.invalidConfiguration
        }
        let mode = values.count == 15 ? values[14] : "normal"
        guard ["normal", "lease-expiry"].contains(mode) else {
            throw RuntimeError.invalidConfiguration
        }
        let qemu = RuntimeAsset(path: values[0], sha256: values[1])
        let qemuFramework = RuntimeAsset(path: values[2], sha256: values[3])
        let kernel = RuntimeAsset(path: values[4], sha256: values[5])
        let initrd = RuntimeAsset(path: values[6], sha256: values[7])
        let root = RuntimeAsset(path: values[8], sha256: values[9])
        let data = URL(fileURLWithPath: values[10])
        let helper = RuntimeAsset(path: values[11], sha256: values[12])
        let privateDirectory = URL(fileURLWithPath: values[13], isDirectory: true)
        let manager = RuntimeManager()
        stage("verify-qemu")
        _ = try manager.verify(qemu, executable: true)
        stage("verify-qemu-framework")
        _ = try manager.verify(qemuFramework, executable: true)
        stage("verify-kernel")
        _ = try manager.verify(kernel)
        stage("verify-initrd")
        _ = try manager.verify(initrd)
        stage("verify-root")
        _ = try manager.verify(root)
        stage("verify-data")
        _ = try RuntimeOpenedFile.open(path: data.path, requirement: .writableData)
        stage("verify-helper")
        _ = try manager.verify(helper, executable: true)
        let standardized = privateDirectory.standardizedFileURL
        FileHandle.standardError.write(Data(
            "directory-input=\(privateDirectory.path) standardized=\(standardized.path) resolved=\(standardized.resolvingSymlinksInPath().path)\n".utf8
        ))
        stage("prepare")
        let prepared = try manager.prepare(
            qemu: qemu,
            qemuFramework: qemuFramework,
            kernel: kernel,
            initrd: initrd,
            root: root,
            data: data,
            privateDirectory: privateDirectory,
            epoch: 1,
            role: "harness"
        )
        stage("start")
        let handle = try manager.start(prepared, helper: helper)
        var terminated = false
        defer {
            if !terminated {
                try? handle.terminate()
            }
        }
        if mode == "lease-expiry" {
            Thread.sleep(forTimeInterval: 7)
            for _ in 0..<2 {
                do {
                    try handle.heartbeat()
                    throw RuntimeError.invalidControl
                } catch RuntimeError.invalidControl {
                    throw RuntimeError.invalidControl
                } catch {
                    stage("lease-rejected")
                }
            }
            try handle.terminate()
            terminated = true
            print("HB-11 real lease expiry remained terminal")
            return
        }
        for _ in 0..<8 {
            Thread.sleep(forTimeInterval: 1)
            stage("heartbeat")
            try handle.heartbeat()
        }
        stage("terminate")
        try handle.terminate()
        terminated = true
        print("HB-11 real RuntimeManager/launchd/helper/QEMU integration passed")
    }
}
