import SwiftUI
import HavenboxCore

@main
struct HavenboxApp: App {
    var body: some Scene {
        WindowGroup("Havenbox — Engineering Candidate") {
            VStack(alignment: .leading, spacing: 16) {
                Text("Havenbox").font(.largeTitle)
                Text(EngineeringBuild.status)
                Text("仅工程骨架。尚未提供工作空间、VM、推理或文件处理；当前组合不可运行。")
                Text("协议 hb.channel.v\(ProtocolHeader.version) · 不下载运行依赖")
            }
            .padding(32)
            .frame(minWidth: 560, minHeight: 240)
        }
    }
}
