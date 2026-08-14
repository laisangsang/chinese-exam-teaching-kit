#!/usr/bin/env swift

import AppKit
import Foundation
import Vision


func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("error: \(message)\n".utf8))
    exit(1)
}

guard CommandLine.arguments.count == 3 else {
    fail("usage: swift apple_vision_ocr.swift INPUT_DIRECTORY OUTPUT_MARKDOWN")
}

let inputDirectory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2])
let fileManager = FileManager.default
let imageExtensions = Set(["png", "jpg", "jpeg", "tif", "tiff"])

let imageURLs: [URL]
do {
    imageURLs = try fileManager.contentsOfDirectory(
        at: inputDirectory,
        includingPropertiesForKeys: nil,
        options: [.skipsHiddenFiles]
    )
    .filter { imageExtensions.contains($0.pathExtension.lowercased()) }
    .sorted {
        $0.lastPathComponent.compare(
            $1.lastPathComponent,
            options: [.numeric, .caseInsensitive]
        ) == .orderedAscending
    }
} catch {
    fail("cannot read input directory")
}

guard !imageURLs.isEmpty else {
    fail("no page images found")
}

var pages: [String] = []
for (index, imageURL) in imageURLs.enumerated() {
    guard let image = NSImage(contentsOf: imageURL),
          let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        fail("cannot decode page image")
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = true

    do {
        try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
    } catch {
        fail("OCR failed")
    }

    let observations = (request.results ?? []).sorted {
        let verticalDifference = $0.boundingBox.midY - $1.boundingBox.midY
        if abs(verticalDifference) > 0.008 {
            return verticalDifference > 0
        }
        return $0.boundingBox.minX < $1.boundingBox.minX
    }
    let text = observations.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
    pages.append("<!-- page:\(index + 1) -->\n\n\(text)")
}

do {
    try fileManager.createDirectory(
        at: outputURL.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    try (pages.joined(separator: "\n\n") + "\n").write(
        to: outputURL,
        atomically: true,
        encoding: .utf8
    )
} catch {
    fail("cannot write OCR output")
}
