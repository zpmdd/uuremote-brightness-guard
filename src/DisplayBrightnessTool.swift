import AppKit
import CoreGraphics
import Dispatch
import Foundation
import IOKit

private let ddcAddress: UInt8 = 0x37
private let ddcDataAddress: UInt8 = 0x51
private let brightnessVCP: UInt8 = 0x10

private struct NativeSnapshot: Codable {
  let slot: Int
  let displayID: UInt32
  let value: Float?
}

private struct GammaSnapshot: Codable {
  let slot: Int
  let displayID: UInt32
  let sampleCount: UInt32
  let red: [Float]
  let green: [Float]
  let blue: [Float]
  let captured: Bool?
}

private struct DDCSnapshot: Codable {
  let slot: Int
  let current: UInt16?
  let maximum: UInt16?
  var registryID: UInt64?
  var captureDetail: String?
}

private struct DDCBaseline: Codable {
  let bootSession: String
  let displays: [DDCSnapshot]
}

private struct BrightnessSnapshot: Codable {
  let schemaVersion: Int
  let createdAt: String
  let native: [NativeSnapshot]
  let gamma: [GammaSnapshot]
  let ddc: [DDCSnapshot]
}

private struct TargetResult: Codable {
  let target: String
  let readable: Bool?
  let requested: Double?
  let observed: Double?
  let applied: Bool
  let fallbackUsed: Bool
  let detail: String?
}

private struct CommandResult: Codable {
  let action: String
  let success: Bool
  let native: [TargetResult]
  let gamma: [TargetResult]
  let ddc: [TargetResult]
  let warnings: [String]
}

private struct RuntimeDDCService {
  let slot: Int
  let registryID: UInt64?
  let service: IOAVService?
}

private struct ScreenEvent: Codable {
  let action: String
  let event: String
}

private final class ScreenEventObserver: NSObject {
  @objc func screensDidSleep(_ notification: Notification) {
    emitScreenEvent("sleep")
  }

  @objc func screensDidWake(_ notification: Notification) {
    emitScreenEvent("wake")
  }
}

private enum ToolError: Error, CustomStringConvertible {
  case usage(String)
  case noDisplays

  var description: String {
    switch self {
    case let .usage(message): return message
    case .noDisplays: return "No online displays were found"
    }
  }
}

private func checksum(seed: UInt8, data: [UInt8], end: Int) -> UInt8 {
  var value = seed
  guard end >= 0 else { return value }
  for index in 0 ... end {
    value ^= data[index]
  }
  return value
}

private func parseDDCReply(
  _ reply: [UInt8],
  expectedVCP: UInt8
) -> (current: UInt16, maximum: UInt16)? {
  // Get VCP reply: source, length, reply opcode, status, VCP, type, max, current, checksum.
  guard reply.count == 11,
        reply[0] == ddcAddress << 1,
        reply[1] & 0x7f == 8,
        reply[2] == 0x02,
        reply[3] == 0,
        reply[4] == expectedVCP,
        reply[5] == 0,
        checksum(seed: 0x50, data: reply, end: 9) == reply[10]
  else { return nil }
  let maximum = UInt16(reply[6]) << 8 | UInt16(reply[7])
  let current = UInt16(reply[8]) << 8 | UInt16(reply[9])
  guard maximum > 0, current <= maximum else { return nil }
  return (current, maximum)
}

private func performDDC(
  service: IOAVService?,
  send: [UInt8],
  replyLength: Int,
  retries: Int = 3
) -> [UInt8]? {
  guard let service else { return nil }
  var packet = [UInt8(0x80 | UInt8(send.count + 1)), UInt8(send.count)] + send + [0]
  let checksumSeed = send.count == 1 ? ddcAddress << 1 : (ddcAddress << 1) ^ ddcDataAddress
  packet[packet.count - 1] = checksum(seed: checksumSeed, data: packet, end: packet.count - 2)

  for _ in 0 ... retries {
    var writeSucceeded = false
    for _ in 0 ..< 2 {
      usleep(10_000)
      writeSucceeded = IOAVServiceWriteI2C(
        service,
        UInt32(ddcAddress),
        UInt32(ddcDataAddress),
        &packet,
        UInt32(packet.count)
      ) == KERN_SUCCESS
    }

    guard writeSucceeded else {
      usleep(20_000)
      continue
    }
    guard replyLength > 0 else { return [] }

    var reply = [UInt8](repeating: 0, count: replyLength)
    usleep(50_000)
    if IOAVServiceReadI2C(service, UInt32(ddcAddress), 0, &reply, UInt32(reply.count)) == KERN_SUCCESS,
       parseDDCReply(reply, expectedVCP: send[0]) != nil
    {
      return reply
    }
    usleep(20_000)
  }
  return nil
}

private func readDDCBrightness(_ service: IOAVService?) -> (current: UInt16, maximum: UInt16)? {
  guard let reply = performDDC(service: service, send: [brightnessVCP], replyLength: 11) else { return nil }
  return parseDDCReply(reply, expectedVCP: brightnessVCP)
}

private func writeDDCBrightness(_ service: IOAVService?, value: UInt16) -> Bool {
  let send = [brightnessVCP, UInt8(value >> 8), UInt8(value & 0xff)]
  return performDDC(service: service, send: send, replyLength: 0) != nil
}

private func nextObject(
  matching interests: [String],
  iterator: inout io_iterator_t
) -> (name: String, entry: io_registry_entry_t)? {
  while true {
    let entry = IOIteratorNext(iterator)
    guard entry != IO_OBJECT_NULL else { return nil }
    var nameBuffer = [CChar](repeating: 0, count: 128)
    guard IORegistryEntryGetName(entry, &nameBuffer) == KERN_SUCCESS else {
      IOObjectRelease(entry)
      continue
    }
    let name = String(cString: nameBuffer)
    if interests.contains(where: { name.contains($0) }) {
      return (name, entry)
    }
    IOObjectRelease(entry)
  }
}

private func discoverDDCServices() -> [RuntimeDDCService] {
  var iterator = io_iterator_t()
  let root = IORegistryGetRootEntry(kIOMainPortDefault)
  defer {
    IOObjectRelease(iterator)
    IOObjectRelease(root)
  }
  guard IORegistryEntryCreateIterator(
    root,
    kIOServicePlane,
    IOOptionBits(kIORegistryIterateRecursively),
    &iterator
  ) == KERN_SUCCESS else { return [] }

  let framebufferNames = ["AppleCLCD2", "IOMobileFramebufferShim"]
  var candidates: [RuntimeDDCService] = []
  var slot = 0
  while let object = nextObject(
    matching: ["DCPAVServiceProxy"] + framebufferNames,
    iterator: &iterator
  ) {
    defer { IOObjectRelease(object.entry) }
    if framebufferNames.contains(object.name) {
      continue
    }
    guard object.name == "DCPAVServiceProxy",
          let locationValue = IORegistryEntryCreateCFProperty(
            object.entry,
            "Location" as CFString,
            kCFAllocatorDefault,
            IOOptionBits(kIORegistryIterateRecursively)
          )?.takeRetainedValue() as? String,
          locationValue == "External"
    else { continue }

    let service = IOAVServiceCreateWithService(kCFAllocatorDefault, object.entry)?.takeRetainedValue()
    var registryID: UInt64 = 0
    let identified = IORegistryEntryGetRegistryEntryID(object.entry, &registryID) == KERN_SUCCESS
    candidates.append(RuntimeDDCService(
      slot: slot, registryID: identified ? registryID : nil, service: service
    ))
    slot += 1
  }
  return candidates
}

private func onlineDisplayIDs() throws -> [CGDirectDisplayID] {
  var count: UInt32 = 0
  guard CGGetOnlineDisplayList(0, nil, &count) == .success, count > 0 else {
    throw ToolError.noDisplays
  }
  var displays = [CGDirectDisplayID](repeating: 0, count: Int(count))
  guard CGGetOnlineDisplayList(count, &displays, &count) == .success else {
    throw ToolError.noDisplays
  }
  return Array(displays.prefix(Int(count)))
}

private func captureGamma(displayID: CGDirectDisplayID, slot: Int) -> GammaSnapshot? {
  let capacity = 256
  var red = [CGGammaValue](repeating: 0, count: capacity)
  var green = [CGGammaValue](repeating: 0, count: capacity)
  var blue = [CGGammaValue](repeating: 0, count: capacity)
  var sampleCount: UInt32 = 0
  guard CGGetDisplayTransferByTable(
    displayID,
    UInt32(capacity),
    &red,
    &green,
    &blue,
    &sampleCount
  ) == .success, sampleCount > 0, sampleCount <= capacity else { return nil }
  let samples = Int(sampleCount)
  let capturedRed = Array(red.prefix(samples))
  let capturedGreen = Array(green.prefix(samples))
  let capturedBlue = Array(blue.prefix(samples))
  guard capturedRed.allSatisfy(\.isFinite),
        capturedGreen.allSatisfy(\.isFinite),
        capturedBlue.allSatisfy(\.isFinite)
  else { return nil }
  return GammaSnapshot(
    slot: slot,
    displayID: displayID,
    sampleCount: sampleCount,
    red: capturedRed,
    green: capturedGreen,
    blue: capturedBlue,
    captured: true
  )
}

private func linearGamma(displayID: CGDirectDisplayID, slot: Int) -> GammaSnapshot {
  let sampleCount: UInt32 = 256
  let values = (0 ..< Int(sampleCount)).map { Float($0) / Float(sampleCount - 1) }
  return GammaSnapshot(
    slot: slot,
    displayID: displayID,
    sampleCount: sampleCount,
    red: values,
    green: values,
    blue: values,
    captured: false
  )
}

private func applyGamma(_ snapshot: GammaSnapshot, factor: Float) -> Bool {
  guard snapshot.sampleCount > 0,
        snapshot.red.count == Int(snapshot.sampleCount),
        snapshot.green.count == Int(snapshot.sampleCount),
        snapshot.blue.count == Int(snapshot.sampleCount),
        snapshot.red.allSatisfy(\.isFinite),
        snapshot.green.allSatisfy(\.isFinite),
        snapshot.blue.allSatisfy(\.isFinite)
  else { return false }
  let clamped = max(0, min(1, factor))
  let red = snapshot.red.map { $0 * clamped }
  let green = snapshot.green.map { $0 * clamped }
  let blue = snapshot.blue.map { $0 * clamped }
  return CGSetDisplayTransferByTable(
    snapshot.displayID,
    snapshot.sampleCount,
    red,
    green,
    blue
  ) == .success
}

private func selectDDCRestoreValue(
  _ captured: DDCSnapshot,
  previous: [DDCSnapshot],
  protectZero: Bool
) -> DDCSnapshot {
  let valid = captured.current != nil && (captured.maximum ?? 0) > 0
    && captured.current! <= captured.maximum!
  if valid && (!protectZero || captured.current != 0) {
    return captured
  }
  // Registry IDs are only comparable within one boot; never reuse a cache by slot.
  let saved = previous.first {
    captured.registryID != nil && $0.registryID == captured.registryID
      && $0.current != nil && ($0.maximum ?? 0) > 0 && $0.current! <= $0.maximum!
      && (captured.maximum == nil || $0.maximum == captured.maximum)
  }
  let source = saved == nil ? "fallback" : "last-good"
  return DDCSnapshot(
    slot: captured.slot,
    current: saved?.current,
    maximum: saved?.maximum ?? ((captured.maximum ?? 0) > 0 ? captured.maximum : nil),
    registryID: captured.registryID,
    captureDetail: "snapshot source=\(source), captured=\(captured.current.map(String.init) ?? "unreadable"), restore=\((saved?.current).map(String.init) ?? "fallback")"
  )
}

private func captureSnapshot(
  protectZero: Bool = false,
  previousDDC: [DDCSnapshot] = []
) throws -> BrightnessSnapshot {
  let displays = try onlineDisplayIDs()
  var native: [NativeSnapshot] = []
  var gamma: [GammaSnapshot] = []

  for (slot, displayID) in displays.enumerated() {
    if CGDisplayIsBuiltin(displayID) != 0 {
      var value: Float = 0
      let readable = DisplayServicesGetBrightness(displayID, &value) == 0
      native.append(NativeSnapshot(slot: slot, displayID: displayID, value: readable ? value : nil))
    } else {
      gamma.append(
        captureGamma(displayID: displayID, slot: slot)
          ?? linearGamma(displayID: displayID, slot: slot)
      )
    }
  }

  let ddc = discoverDDCServices().map { runtime -> DDCSnapshot in
    var value = readDDCBrightness(runtime.service)
    var retried = false
    if protectZero && (value == nil || value?.current == 0) {
      // One short re-read, not a wait-for-wake loop that delays privacy dimming.
      usleep(120_000)
      let retry = readDDCBrightness(runtime.service)
      if let retry, retry.current > 0 { value = retry }
      retried = true
    }
    return selectDDCRestoreValue(DDCSnapshot(
      slot: runtime.slot, current: value?.current, maximum: value?.maximum,
      registryID: runtime.registryID,
      captureDetail: "snapshot source=\(retried ? "capture-retry" : "capture"), restore=\((value?.current).map(String.init) ?? "unreadable")"
    ), previous: previousDDC, protectZero: protectZero)
  }

  let formatter = ISO8601DateFormatter()
  return BrightnessSnapshot(
    schemaVersion: 1,
    createdAt: formatter.string(from: Date()),
    native: native,
    gamma: gamma,
    ddc: ddc
  )
}

private func saveSnapshot<T: Encodable>(_ snapshot: T, path: String) throws {
  let encoder = JSONEncoder()
  encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
  let data = try encoder.encode(snapshot)
  let url = URL(fileURLWithPath: path)
  try FileManager.default.createDirectory(
    at: url.deletingLastPathComponent(),
    withIntermediateDirectories: true
  )
  try data.write(to: url, options: .atomic)
  try FileManager.default.setAttributes(
    [.posixPermissions: NSNumber(value: Int16(0o600))],
    ofItemAtPath: path
  )
}

private func loadSnapshot(path: String) throws -> BrightnessSnapshot {
  let data = try Data(contentsOf: URL(fileURLWithPath: path))
  return try JSONDecoder().decode(BrightnessSnapshot.self, from: data)
}

private func bootSession() -> String? {
  var boot = timeval()
  var size = MemoryLayout<timeval>.size
  guard sysctlbyname("kern.boottime", &boot, &size, nil, 0) == 0 else { return nil }
  return "\(boot.tv_sec):\(boot.tv_usec)"
}

private func loadDDCBaseline(path: String?, boot: String?) -> [DDCSnapshot] {
  guard let path, let boot,
        let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
        let saved = try? JSONDecoder().decode(DDCBaseline.self, from: data),
        saved.bootSession == boot
  else { return [] }
  return saved.displays
}

private func captureRestoreSnapshot(
  path: String, baselinePath: String?, protectZero: Bool
) throws -> BrightnessSnapshot {
  let boot = bootSession()
  let previous = loadDDCBaseline(path: baselinePath, boot: boot)
  let snapshot = try captureSnapshot(
    protectZero: protectZero || displaysAreAsleep() != false,
    previousDDC: previous
  )
  try saveSnapshot(snapshot, path: path)
  if let baselinePath, let boot {
    let reliable = snapshot.ddc.filter { $0.current != nil && $0.registryID != nil }
    try saveSnapshot(DDCBaseline(bootSession: boot, displays: reliable), path: baselinePath)
  }
  return snapshot
}

private func setNative(displayID: CGDirectDisplayID, value: Float) -> (Bool, Float?) {
  let clamped = max(0, min(1, value))
  let applied = DisplayServicesSetBrightness(displayID, clamped) == 0
  usleep(40_000)
  var observed: Float = 0
  let readable = DisplayServicesGetBrightness(displayID, &observed) == 0
  return (applied, readable ? observed : nil)
}

private func dim(snapshot: BrightnessSnapshot, factor: Float) -> CommandResult {
  var nativeResults: [TargetResult] = []
  var gammaResults: [TargetResult] = []
  var ddcResults: [TargetResult] = []
  var warnings: [String] = []

  for item in snapshot.native {
    let result = setNative(displayID: item.displayID, value: 0)
    nativeResults.append(TargetResult(
      target: "native-\(item.slot)",
      readable: item.value != nil,
      requested: 0,
      observed: result.1.map(Double.init),
      applied: result.0,
      fallbackUsed: false,
      detail: result.0 ? nil : "native write failed"
    ))
    if !result.0 { warnings.append("native-\(item.slot) could not be dimmed") }
  }

  let services = discoverDDCServices()
  for service in services {
    let applied = writeDDCBrightness(service.service, value: 0)
    usleep(80_000)
    let observed = readDDCBrightness(service.service)
    ddcResults.append(TargetResult(
      target: "ddc-\(service.slot)",
      readable: snapshot.ddc.first(where: { $0.slot == service.slot })?.current != nil,
      requested: 0,
      observed: observed.map { Double($0.current) / Double($0.maximum) },
      applied: applied,
      fallbackUsed: false,
      detail: applied
        ? snapshot.ddc.first(where: { $0.slot == service.slot })?.captureDetail
        : "DDC write failed"
    ))
    if !applied { warnings.append("ddc-\(service.slot) could not be dimmed") }
  }

  for item in snapshot.gamma {
    let applied = applyGamma(item, factor: factor)
    gammaResults.append(TargetResult(
      target: "gamma-\(item.slot)",
      readable: true,
      requested: Double(factor),
      observed: nil,
      applied: applied,
      fallbackUsed: false,
      detail: applied ? nil : "gamma write failed"
    ))
    if !applied { warnings.append("gamma-\(item.slot) could not be dimmed") }
  }

  let changedCount = nativeResults.count + gammaResults.count + ddcResults.count
  return CommandResult(
    action: "dim",
    success: changedCount > 0 && warnings.isEmpty,
    native: nativeResults,
    gamma: gammaResults,
    ddc: ddcResults,
    warnings: warnings
  )
}

private func restore(
  snapshot: BrightnessSnapshot,
  fallback: Float,
  ddcFallback: Float
) throws -> CommandResult {
  let clampedFallback = max(0, min(1, fallback))
  let clampedDDCFallback = max(0, min(1, ddcFallback))
  let displays = try onlineDisplayIDs()
  let nativeDisplays = displays.enumerated().filter { CGDisplayIsBuiltin($0.element) != 0 }
  var nativeResults: [TargetResult] = []
  var gammaResults: [TargetResult] = []
  var ddcResults: [TargetResult] = []
  var warnings: [String] = []

  for (slot, displayID) in nativeDisplays {
    let saved = snapshot.native.first(where: { $0.displayID == displayID })?.value
    let requested = saved ?? clampedFallback
    let result = setNative(displayID: displayID, value: requested)
    nativeResults.append(TargetResult(
      target: "native-\(slot)",
      readable: saved != nil,
      requested: Double(requested),
      observed: result.1.map(Double.init),
      applied: result.0,
      fallbackUsed: saved == nil,
      detail: result.0 ? nil : "native restore failed"
    ))
    if !result.0 { warnings.append("native-\(slot) could not be restored") }
  }

  let services = discoverDDCServices()
  let topologyMatches = services.count == snapshot.ddc.count
  for service in services {
    let saved = topologyMatches ? snapshot.ddc.first(where: { $0.slot == service.slot }) : nil
    let live = readDDCBrightness(service.service)
    let maximum = saved?.maximum ?? live?.maximum ?? 100
    let requested: UInt16
    let useFallback: Bool
    if let current = saved?.current, saved?.maximum != nil {
      requested = min(current, maximum)
      useFallback = false
    } else {
      requested = UInt16((Double(maximum) * Double(clampedDDCFallback)).rounded())
      useFallback = true
    }
    let applied = writeDDCBrightness(service.service, value: requested)
    usleep(80_000)
    let observed = readDDCBrightness(service.service)
    ddcResults.append(TargetResult(
      target: "ddc-\(service.slot)",
      readable: saved?.current != nil,
      requested: Double(requested) / Double(maximum),
      observed: observed.map { Double($0.current) / Double($0.maximum) },
      applied: applied,
      fallbackUsed: useFallback,
      detail: applied ? nil : "DDC restore failed"
    ))
    if !applied { warnings.append("ddc-\(service.slot) could not be restored") }
  }

  let externalDisplays = displays.enumerated().filter { CGDisplayIsBuiltin($0.element) == 0 }
  let gammaTopologyMatches = externalDisplays.count == snapshot.gamma.count
  for (slot, displayID) in externalDisplays {
    let saved = gammaForDisplay(
      snapshot: snapshot,
      displayID: displayID,
      slot: slot,
      topologyMatches: gammaTopologyMatches
    )
    let item = saved ?? linearGamma(displayID: displayID, slot: slot)
    let applied = applyGamma(item, factor: 1)
    let observed = captureGamma(displayID: displayID, slot: slot)
    gammaResults.append(TargetResult(
      target: "gamma-\(slot)",
      readable: saved?.captured != false && saved != nil,
      requested: Double(gammaPeak(item)),
      observed: observed.map { Double(gammaPeak($0)) },
      applied: applied,
      fallbackUsed: saved == nil || saved?.captured == false,
      detail: applied ? nil : "gamma restore failed"
    ))
    if !applied { warnings.append("gamma-\(slot) could not be restored") }
  }

  let changedCount = nativeResults.count + gammaResults.count + ddcResults.count
  return CommandResult(
    action: "restore",
    success: changedCount > 0
      && !nativeResults.contains(where: { !$0.applied })
      && !ddcResults.contains(where: { !$0.applied })
      && !gammaResults.contains(where: { !$0.applied }),
    native: nativeResults,
    gamma: gammaResults,
    ddc: ddcResults,
    warnings: warnings
  )
}

private func gammaPeak(_ snapshot: GammaSnapshot) -> Float {
  max(
    max(snapshot.red.max() ?? 0, snapshot.green.max() ?? 0),
    snapshot.blue.max() ?? 0
  )
}

private func gammaForDisplay(
  snapshot: BrightnessSnapshot,
  displayID: CGDirectDisplayID,
  slot: Int,
  topologyMatches: Bool
) -> GammaSnapshot? {
  let saved = snapshot.gamma.first(where: { $0.displayID == displayID })
    ?? (topologyMatches ? snapshot.gamma.first(where: { $0.slot == slot }) : nil)
  guard let saved else { return nil }
  return GammaSnapshot(
    slot: slot,
    displayID: displayID,
    sampleCount: saved.sampleCount,
    red: saved.red,
    green: saved.green,
    blue: saved.blue,
    captured: saved.captured
  )
}

private func gammaMatches(_ current: GammaSnapshot, _ expected: GammaSnapshot) -> Bool {
  guard current.sampleCount == expected.sampleCount,
        current.red.count == expected.red.count,
        current.green.count == expected.green.count,
        current.blue.count == expected.blue.count
  else { return false }
  let tolerance: Float = 0.01
  for index in current.red.indices {
    if abs(current.red[index] - expected.red[index]) > tolerance
      || abs(current.green[index] - expected.green[index]) > tolerance
      || abs(current.blue[index] - expected.blue[index]) > tolerance
    {
      return false
    }
  }
  return true
}

private func verify(
  snapshot: BrightnessSnapshot,
  fallback: Float,
  ddcFallback: Float
) throws -> CommandResult {
  let clampedFallback = max(0, min(1, fallback))
  let clampedDDCFallback = max(0, min(1, ddcFallback))
  let displays = try onlineDisplayIDs()
  let nativeDisplays = displays.enumerated().filter { CGDisplayIsBuiltin($0.element) != 0 }
  let nativeTopologyMatches = nativeDisplays.count == snapshot.native.count
  var nativeResults: [TargetResult] = []
  var gammaResults: [TargetResult] = []
  var ddcResults: [TargetResult] = []
  var warnings: [String] = []

  for (slot, displayID) in nativeDisplays {
    let saved = snapshot.native.first(where: { $0.displayID == displayID })?.value
    let expected = saved ?? clampedFallback
    var observed: Float = 0
    let readable = DisplayServicesGetBrightness(displayID, &observed) == 0
    let matches = readable && abs(observed - expected) <= 0.02
    nativeResults.append(TargetResult(
      target: "native-\(slot)",
      readable: readable,
      requested: Double(expected),
      observed: readable ? Double(observed) : nil,
      applied: matches,
      fallbackUsed: saved == nil,
      detail: matches ? nil : "native verification mismatch"
    ))
    if !matches { warnings.append("native-\(slot) verification mismatch") }
  }

  let services = discoverDDCServices()
  let topologyMatches = services.count == snapshot.ddc.count
  for service in services {
    let saved = topologyMatches ? snapshot.ddc.first(where: { $0.slot == service.slot }) : nil
    let live = readDDCBrightness(service.service)
    let maximum = saved?.maximum ?? live?.maximum ?? 100
    let expected: UInt16
    let useFallback: Bool
    if let current = saved?.current, saved?.maximum != nil {
      expected = min(current, maximum)
      useFallback = false
    } else {
      expected = UInt16((Double(maximum) * Double(clampedDDCFallback)).rounded())
      useFallback = true
    }
    let matches = live.map { abs(Int($0.current) - Int(expected)) <= 1 } ?? false
    ddcResults.append(TargetResult(
      target: "ddc-\(service.slot)",
      readable: live != nil,
      requested: Double(expected) / Double(maximum),
      observed: live.map { Double($0.current) / Double($0.maximum) },
      applied: matches,
      fallbackUsed: useFallback,
      detail: matches ? nil : "DDC verification mismatch"
    ))
    if !matches { warnings.append("ddc-\(service.slot) verification mismatch") }
  }

  let externalDisplays = displays.enumerated().filter { CGDisplayIsBuiltin($0.element) == 0 }
  let gammaTopologyMatches = externalDisplays.count == snapshot.gamma.count
  for (slot, displayID) in externalDisplays {
    let saved = gammaForDisplay(
      snapshot: snapshot,
      displayID: displayID,
      slot: slot,
      topologyMatches: gammaTopologyMatches
    )
    let expected = saved ?? linearGamma(displayID: displayID, slot: slot)
    let current = captureGamma(displayID: displayID, slot: slot)
    let matches = current.map { gammaMatches($0, expected) } ?? false
    gammaResults.append(TargetResult(
      target: "gamma-\(slot)",
      readable: current != nil,
      requested: Double(gammaPeak(expected)),
      observed: current.map { Double(gammaPeak($0)) },
      applied: matches,
      fallbackUsed: saved == nil || saved?.captured == false,
      detail: matches ? nil : "gamma verification mismatch"
    ))
    if !matches { warnings.append("gamma-\(slot) verification mismatch") }
  }

  if !nativeTopologyMatches { warnings.append("native display topology mismatch") }
  if !topologyMatches { warnings.append("DDC display topology mismatch") }
  if !gammaTopologyMatches { warnings.append("external display topology mismatch") }

  let changedCount = nativeResults.count + gammaResults.count + ddcResults.count
  return CommandResult(
    action: "verify",
    success: changedCount > 0
      && nativeTopologyMatches
      && topologyMatches
      && gammaTopologyMatches
      && !nativeResults.contains(where: { !$0.applied })
      && !ddcResults.contains(where: { !$0.applied })
      && !gammaResults.contains(where: { !$0.applied }),
    native: nativeResults,
    gamma: gammaResults,
    ddc: ddcResults,
    warnings: warnings
  )
}

private func probe() throws -> CommandResult {
  let snapshot = try captureSnapshot()
  let native = snapshot.native.map { item in
    TargetResult(
      target: "native-\(item.slot)",
      readable: item.value != nil,
      requested: nil,
      observed: item.value.map(Double.init),
      applied: false,
      fallbackUsed: false,
      detail: nil
    )
  }
  let gamma = snapshot.gamma.map { item in
    TargetResult(
      target: "gamma-\(item.slot)",
      readable: item.captured != false,
      requested: nil,
      observed: Double(gammaPeak(item)),
      applied: false,
      fallbackUsed: item.captured == false,
      detail: "\(item.sampleCount) finite samples"
    )
  }
  let ddc = snapshot.ddc.map { item in
    TargetResult(
      target: "ddc-\(item.slot)",
      readable: item.current != nil,
      requested: nil,
      observed: item.current.flatMap { current in
        item.maximum.map { Double(current) / Double($0) }
      },
      applied: false,
      fallbackUsed: false,
      detail: nil
    )
  }
  let readableCount = native.filter { $0.readable == true }.count + ddc.filter { $0.readable == true }.count
  return CommandResult(
    action: "probe",
    success: readableCount > 0,
    native: native,
    gamma: gamma,
    ddc: ddc,
    warnings: []
  )
}

private func argument(_ name: String, in arguments: [String]) -> String? {
  guard let index = arguments.firstIndex(of: name), arguments.indices.contains(index + 1) else {
    return nil
  }
  return arguments[index + 1]
}

private func emit(_ result: CommandResult) throws {
  let encoder = JSONEncoder()
  encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
  FileHandle.standardOutput.write(try encoder.encode(result))
  FileHandle.standardOutput.write(Data("\n".utf8))
}

private func emitCompact(_ result: CommandResult) throws {
  let encoder = JSONEncoder()
  FileHandle.standardOutput.write(try encoder.encode(result))
  FileHandle.standardOutput.write(Data("\n".utf8))
}

private func processExists(_ pid: pid_t) -> Bool {
  guard pid > 1 else { return true }
  if kill(pid, 0) == 0 { return true }
  return errno == EPERM
}

private func emitScreenEvent(_ event: String) {
  let payload = ScreenEvent(action: "events", event: event)
  guard let data = try? JSONEncoder().encode(payload) else { return }
  FileHandle.standardOutput.write(data)
  FileHandle.standardOutput.write(Data("\n".utf8))
}

private func displaysAreAsleep() -> Bool? {
  guard let displays = try? onlineDisplayIDs(), !displays.isEmpty else { return nil }
  return displays.allSatisfy { CGDisplayIsAsleep($0) != 0 }
}

private func monitorScreenEvents(parentPID: pid_t) {
  let observer = ScreenEventObserver()
  let center = NSWorkspace.shared.notificationCenter
  center.addObserver(
    observer,
    selector: #selector(ScreenEventObserver.screensDidSleep(_:)),
    name: NSWorkspace.screensDidSleepNotification,
    object: nil
  )
  center.addObserver(
    observer,
    selector: #selector(ScreenEventObserver.screensDidWake(_:)),
    name: NSWorkspace.screensDidWakeNotification,
    object: nil
  )
  defer { center.removeObserver(observer) }

  emitScreenEvent("ready")
  var lastPolledState = displaysAreAsleep()
  if let lastPolledState {
    emitScreenEvent(lastPolledState ? "sleep" : "wake")
  }
  while processExists(parentPID) {
    RunLoop.current.run(until: Date().addingTimeInterval(0.25))
    let currentState = displaysAreAsleep()
    if let currentState, let previousState = lastPolledState, currentState != previousState {
      emitScreenEvent(currentState ? "sleep" : "wake")
    }
    lastPolledState = currentState
  }
}

private func holdDimmed(
  snapshotPath: String,
  factor: Float,
  fallback: Float,
  ddcFallback: Float,
  parentPID: pid_t,
  reuseSnapshot: Bool,
  baselinePath: String?,
  protectZero: Bool
) throws -> CommandResult {
  let snapshot: BrightnessSnapshot
  if reuseSnapshot {
    snapshot = try loadSnapshot(path: snapshotPath)
  } else {
    snapshot = try captureRestoreSnapshot(
      path: snapshotPath, baselinePath: baselinePath, protectZero: protectZero
    )
  }

  signal(SIGTERM, SIG_IGN)
  signal(SIGINT, SIG_IGN)
  var shouldStop = false
  let termSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
  let intSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
  termSource.setEventHandler { shouldStop = true }
  intSource.setEventHandler { shouldStop = true }
  termSource.resume()
  intSource.resume()

  let initialResult = dim(snapshot: snapshot, factor: factor)
  try emitCompact(initialResult)

  var nextGammaRefresh = Date().addingTimeInterval(0.5)
  var missingParentChecks = 0
  while !shouldStop {
    RunLoop.current.run(until: Date().addingTimeInterval(0.2))
    if Date() >= nextGammaRefresh {
      for item in snapshot.gamma {
        _ = applyGamma(item, factor: factor)
      }
      nextGammaRefresh = Date().addingTimeInterval(0.5)
      if parentPID > 1, !processExists(parentPID) {
        missingParentChecks += 1
        if missingParentChecks >= 3 {
          shouldStop = true
        }
      } else {
        missingParentChecks = 0
      }
    }
  }

  termSource.cancel()
  intSource.cancel()
  return try restore(snapshot: snapshot, fallback: fallback, ddcFallback: ddcFallback)
}

private func run() throws {
  let arguments = Array(CommandLine.arguments.dropFirst())
  guard let command = arguments.first else {
    throw ToolError.usage("Usage: DisplayBrightnessTool probe | events [--parent-pid PID] | dim --snapshot PATH [--dim-factor 0.0] | hold --snapshot PATH [--reuse-snapshot] [--parent-pid PID] [--fallback 0.85] [--ddc-fallback 0.70] | restore --snapshot PATH [--fallback 0.85] [--ddc-fallback 0.70] | verify --snapshot PATH [--fallback 0.85] [--ddc-fallback 0.70] | fallback [--value 0.85] [--ddc-value 0.70]")
  }

  switch command {
  case "probe":
    try emit(probe())
  case "events":
    let parentPID = pid_t(argument("--parent-pid", in: arguments) ?? "0") ?? 0
    monitorScreenEvents(parentPID: parentPID)
  case "dim":
    guard let path = argument("--snapshot", in: arguments) else {
      throw ToolError.usage("dim requires --snapshot PATH")
    }
    let factor = Float(argument("--dim-factor", in: arguments) ?? "0.0") ?? 0.0
    let snapshot = try captureRestoreSnapshot(
      path: path, baselinePath: argument("--last-good", in: arguments),
      protectZero: arguments.contains("--protect-wake-snapshot")
    )
    try emit(dim(snapshot: snapshot, factor: factor))
  case "hold":
    guard let path = argument("--snapshot", in: arguments) else {
      throw ToolError.usage("hold requires --snapshot PATH")
    }
    let factor = Float(argument("--dim-factor", in: arguments) ?? "0.0") ?? 0.0
    let fallback = Float(argument("--fallback", in: arguments) ?? "0.85") ?? 0.85
    let ddcFallback = Float(argument("--ddc-fallback", in: arguments) ?? "0.70") ?? 0.70
    let parentPID = pid_t(argument("--parent-pid", in: arguments) ?? "0") ?? 0
    let reuseSnapshot = arguments.contains("--reuse-snapshot")
    try emit(holdDimmed(
      snapshotPath: path,
      factor: factor,
      fallback: fallback,
      ddcFallback: ddcFallback,
      parentPID: parentPID,
      reuseSnapshot: reuseSnapshot,
      baselinePath: argument("--last-good", in: arguments),
      protectZero: arguments.contains("--protect-wake-snapshot")
    ))
  case "restore":
    guard let path = argument("--snapshot", in: arguments) else {
      throw ToolError.usage("restore requires --snapshot PATH")
    }
    let fallback = Float(argument("--fallback", in: arguments) ?? "0.85") ?? 0.85
    let ddcFallback = Float(argument("--ddc-fallback", in: arguments) ?? "0.70") ?? 0.70
    try emit(restore(
      snapshot: loadSnapshot(path: path),
      fallback: fallback,
      ddcFallback: ddcFallback
    ))
  case "verify":
    guard let path = argument("--snapshot", in: arguments) else {
      throw ToolError.usage("verify requires --snapshot PATH")
    }
    let fallback = Float(argument("--fallback", in: arguments) ?? "0.85") ?? 0.85
    let ddcFallback = Float(argument("--ddc-fallback", in: arguments) ?? "0.70") ?? 0.70
    try emit(verify(
      snapshot: loadSnapshot(path: path),
      fallback: fallback,
      ddcFallback: ddcFallback
    ))
  case "fallback":
    let value = Float(argument("--value", in: arguments) ?? "0.85") ?? 0.85
    let ddcValue = Float(argument("--ddc-value", in: arguments) ?? "0.70") ?? 0.70
    let emptySnapshot = BrightnessSnapshot(
      schemaVersion: 1,
      createdAt: ISO8601DateFormatter().string(from: Date()),
      native: [],
      gamma: [],
      ddc: []
    )
    try emit(restore(snapshot: emptySnapshot, fallback: value, ddcFallback: ddcValue))
  default:
    throw ToolError.usage("Unknown command: \(command)")
  }
}

#if DDC_REPLY_SELF_TEST
private func checkDDCReplyParsing() {
  let valid: [UInt8] = [0x6e, 0x88, 0x02, 0, 0x10, 0, 0, 100, 0, 70, 0x86]
  let parsed = parseDDCReply(valid, expectedVCP: brightnessVCP)
  precondition(parsed?.current == 70 && parsed?.maximum == 100, "valid brightness reply")
  for (index, value): (Int, UInt8) in [
    (0, 0x50), // wrong source
    (1, 0x87), // wrong payload length
    (2, 0x01), // request instead of reply
    (3, 0x01), // unsupported VCP
    (4, 0x62), // valid volume reply must not become brightness
    (5, 0x01), // momentary VCP instead of continuous brightness
    (7, 0), // zero maximum
    (9, 101), // current exceeds maximum
  ] {
    var invalid = valid
    invalid[index] = value
    invalid[10] = checksum(seed: 0x50, data: invalid, end: 9)
    precondition(parseDDCReply(invalid, expectedVCP: brightnessVCP) == nil, "invalid field \(index)")
  }
  var badChecksum = valid
  badChecksum[10] ^= 1
  precondition(parseDDCReply(badChecksum, expectedVCP: brightnessVCP) == nil, "bad checksum")
  precondition(parseDDCReply(Array(valid.dropLast()), expectedVCP: brightnessVCP) == nil, "truncated reply")
  precondition(parseDDCReply(valid + [0], expectedVCP: brightnessVCP) == nil, "overlong reply")
  print("DDC reply parsing checks passed (no hardware access)")
}
checkDDCReplyParsing()
private func checkWakeSnapshotProtection() throws {
  let previous = [
    DDCSnapshot(slot: 0, current: 70, maximum: 100, registryID: 10),
    DDCSnapshot(slot: 1, current: 60, maximum: 100, registryID: 20),
  ]
  let zero = DDCSnapshot(slot: 0, current: 0, maximum: 100, registryID: 10)
  precondition(selectDDCRestoreValue(zero, previous: previous, protectZero: true).current == 70,
               "wake zero must not replace the last good brightness")
  precondition(selectDDCRestoreValue(zero, previous: previous, protectZero: false).current == 0,
               "a deliberate zero outside the wake window must remain zero")
  let missing = selectDDCRestoreValue(zero, previous: [], protectZero: true)
  precondition(missing.current == nil && missing.maximum == 100,
               "without a baseline, restore and verify must both use their existing fallback")
  let moved = DDCSnapshot(slot: 0, current: 0, maximum: 100, registryID: 20)
  precondition(selectDDCRestoreValue(moved, previous: previous, protectZero: true).current == 60,
               "match by registry identity, not reordered slots")
  for identity: UInt64? in [nil, 30] {
    let unknown = DDCSnapshot(slot: 0, current: 0, maximum: 100, registryID: identity)
    precondition(selectDDCRestoreValue(unknown, previous: previous, protectZero: true).current == nil,
                 "unknown displays must not inherit another display's brightness")
  }
  let changedMaximum = DDCSnapshot(slot: 0, current: 0, maximum: 255, registryID: 10)
  precondition(selectDDCRestoreValue(changedMaximum, previous: previous, protectZero: true).current == nil,
               "a changed DDC scale invalidates the cached value")
  let unreadable = DDCSnapshot(slot: 0, current: nil, maximum: nil, registryID: 10)
  precondition(selectDDCRestoreValue(unreadable, previous: previous, protectZero: true).current == 70,
               "an unreadable value may use the identified baseline")
  let manual = DDCSnapshot(slot: 0, current: 80, maximum: 100, registryID: 10)
  precondition(selectDDCRestoreValue(manual, previous: previous, protectZero: true).current == 80,
               "a readable nonzero value takes precedence over history")
  precondition(selectDDCRestoreValue(zero, previous: [zero], protectZero: true).current == 0,
               "a previously trusted intentional zero remains valid")

  let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
  defer { try? FileManager.default.removeItem(at: directory) }
  let path = directory.appendingPathComponent("ddc-last-good.json").path
  try saveSnapshot(DDCBaseline(bootSession: "boot-A", displays: previous), path: path)
  precondition(loadDDCBaseline(path: path, boot: "boot-A").count == 2, "same-boot baseline")
  precondition(loadDDCBaseline(path: path, boot: "boot-B").isEmpty, "ignore a previous boot's registry IDs")
  precondition(loadDDCBaseline(path: path, boot: nil).isEmpty, "unknown boot fails safe")
  precondition(loadDDCBaseline(path: path + ".missing", boot: "boot-A").isEmpty, "missing cache fails safe")
  let mode = try FileManager.default.attributesOfItem(atPath: path)[.posixPermissions] as? NSNumber
  precondition(mode?.intValue == 0o600, "private baseline permissions")
  // Existing on-disk recovery snapshots remain readable after the upgrade.
  let legacy = Data("{\"slot\":0,\"current\":70,\"maximum\":100}".utf8)
  let decoded = try JSONDecoder().decode(DDCSnapshot.self, from: legacy)
  precondition(decoded.current == 70 && decoded.registryID == nil, "legacy snapshot compatibility")
  print("Wake snapshot protection checks passed (no hardware access)")
}
try checkWakeSnapshotProtection()
#else
do {
  try run()
} catch {
  let message = "DisplayBrightnessTool: \(error)\n"
  FileHandle.standardError.write(Data(message.utf8))
  exit(2)
}
#endif
