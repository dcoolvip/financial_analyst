// Text of a PDF via macOS PDFKit, for statements whose fonts the Python reader can't decode (Chase).
//   pdfkit_text <file.pdf>          plain text, page by page
//   pdfkit_text --lines <file.pdf>  one line per piece of text: page <TAB> y-from-top <TAB> x <TAB> text
import Foundation
import PDFKit

let args = Array(CommandLine.arguments.dropFirst())
let lines = args.first == "--lines"
guard let path = args.last, let doc = PDFDocument(url: URL(fileURLWithPath: path)) else { exit(1) }
for i in 0..<doc.pageCount {
    guard let page = doc.page(at: i) else { continue }
    if !lines { print(page.string ?? ""); continue }
    let box = page.bounds(for: .mediaBox)
    guard let all = page.selection(for: box) else { continue }
    for line in all.selectionsByLine() {
        let b = line.bounds(for: page)
        let text = (line.string ?? "").replacingOccurrences(of: "\n", with: " ")
        if !text.trimmingCharacters(in: .whitespaces).isEmpty {
            print("\(i)\t\(Int(box.height - b.maxY))\t\(Int(b.minX))\t\(text)")
        }
    }
}
