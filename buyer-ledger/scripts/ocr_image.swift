// Local Vision OCR candidate generator for buyer-ledger bill-candidate add
// --image (V-08 golden chain front door). Independent Verovo implementation
// of the local Vision OCR approach; it has no Askovo import or runtime
// dependency. Per-line confidence and normalized bounding boxes let every
// extracted bill field point back at a region of the screenshot.
//
// Output: one JSON object per line --
//   {"text": "...", "confidence": 0.97, "bbox": [x, y, w, h]}
// bbox is normalized (0..1, Vision's lower-left origin) — exactly the shape
// verovo.ocr-lines/1 stores. The candidate that consumes this output stays
// an unreviewed draft: OCR never books anything by itself.

import Foundation
import AppKit
import Vision

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: ocr_image.swift <image>\n".data(using: .utf8)!)
    exit(2)
}
let path = args[1]
guard let image = NSImage(contentsOfFile: path),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot read image\n".data(using: .utf8)!)
    exit(2)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["zh-Hans", "en-US"]

let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([request])
    let observations = request.results ?? []
    if observations.isEmpty {
        FileHandle.standardError.write("no text recognized\n".data(using: .utf8)!)
        exit(2)
    }
    var lines: [String] = []
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        let payload: [String: Any] = [
            "text": candidate.string,
            "confidence": Double(candidate.confidence),
            "bbox": [box.origin.x, box.origin.y, box.width, box.height],
        ]
        let data = try JSONSerialization.data(withJSONObject: payload)
        lines.append(String(data: data, encoding: .utf8) ?? "")
    }
    print(lines.joined(separator: "\n"))
} catch {
    FileHandle.standardError.write("ocr error: \(error)\n".data(using: .utf8)!)
    exit(2)
}
