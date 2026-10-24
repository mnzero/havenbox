import Foundation
import HavenboxCore

if CommandLine.arguments == [CommandLine.arguments[0], "--version"] {
    print("havenbox-helper 0.1.0-candidate · hb.channel.v\(ProtocolHeader.version)")
} else {
    FileHandle.standardError.write(Data("Runtime watchdog is not implemented (HB-11); no process started.\n".utf8))
    exit(78)
}
