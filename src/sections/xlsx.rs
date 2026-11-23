use std::fmt::Write;

fn crc32(bytes: &[u8]) -> u32 {
    let mut crc = u32::MAX;
    for &b in bytes {
        crc ^= b as u32;
        for _ in 0..8 {
            crc = if crc & 1 == 1 {
                (crc >> 1) ^ 0xedb88320
            } else {
                crc >> 1
            };
        }
    }
    !crc
}

fn u16le(out: &mut Vec<u8>, v: usize) {
    out.extend_from_slice(&(v as u16).to_le_bytes());
}
fn u32le(out: &mut Vec<u8>, v: u32) {
    out.extend_from_slice(&v.to_le_bytes());
}

// OOXML is a ZIP package. Store these small worksheets uncompressed so export
// also works without a compression service or an external library in the browser.
fn archive(files: &[(&str, &str)]) -> Vec<u8> {
    let mut out = vec![];
    let mut directory = vec![];
    for &(name, xml) in files {
        let offset = out.len();
        let content = xml.as_bytes();
        let checksum = crc32(content);
        let mut info = vec![];
        for value in [20, 0, 0, 0, 33] {
            u16le(&mut info, value);
        }
        for value in [checksum, content.len() as u32, content.len() as u32] {
            u32le(&mut info, value);
        }
        u16le(&mut info, name.len());
        u16le(&mut info, 0);
        u32le(&mut out, 0x04034b50);
        out.extend(&info);
        out.extend(name.as_bytes());
        out.extend(content);
        u32le(&mut directory, 0x02014b50);
        u16le(&mut directory, 20);
        directory.extend(&info);
        for _ in 0..3 {
            u16le(&mut directory, 0);
        }
        u32le(&mut directory, 0);
        u32le(&mut directory, offset as u32);
        directory.extend(name.as_bytes());
    }
    let position = out.len();
    out.extend(&directory);
    u32le(&mut out, 0x06054b50);
    for v in [0, 0, files.len(), files.len()] {
        u16le(&mut out, v);
    }
    u32le(&mut out, directory.len() as u32);
    u32le(&mut out, position as u32);
    u16le(&mut out, 0);
    out
}
fn base64(data: &[u8]) -> String {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let n = (chunk[0] as u32) << 16
            | (chunk.get(1).copied().unwrap_or(0) as u32) << 8
            | chunk.get(2).copied().unwrap_or(0) as u32;
        for (i, shift) in [18, 12, 6, 0].into_iter().enumerate() {
            out.push(if i > chunk.len() {
                '='
            } else {
                ALPHABET[((n >> shift) & 63) as usize] as char
            });
        }
    }
    out
}
fn column(mut n: usize) -> String {
    let mut letters = vec![];
    while n != 0 {
        n -= 1;
        letters.push((b'A' + (n % 26) as u8) as char);
        n /= 26;
    }
    letters.into_iter().rev().collect()
}
const TYPES: &str = r#"<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>"#;
const ROOT_REL: &str = r#"<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>"#;
const BOOK_REL: &str = r#"<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>"#;

pub(super) fn workbook(title: &str, headers: &[&str], rows: &[Vec<String>]) -> Vec<u8> {
    let mut sheet = String::from(
        r#"<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane xSplit="1" ySplit="1" topLeftCell="B2" activePane="bottomRight" state="frozen"/></sheetView></sheetViews><cols>"#,
    );
    for (i, h) in headers.iter().enumerate() {
        let w = if i == 0 || h.contains("Название") || h.contains("Маршрут") {
            38
        } else {
            22
        };
        write!(
            sheet,
            "<col min=\"{}\" max=\"{}\" width=\"{w}\" customWidth=\"1\"/>",
            i + 1,
            i + 1
        )
        .unwrap();
    }
    sheet.push_str("</cols><sheetData>");
    let headings = headers.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    for (r, row) in std::iter::once(&headings).chain(rows).enumerate() {
        write!(sheet, "<row r=\"{}\">", r + 1).unwrap();
        for (c, text) in row.iter().enumerate().filter(|(_, t)| !t.is_empty()) {
            let cell = format!("{}{}", column(c + 1), r + 1);
            if r > 0 && headers[c] == "Поручений" && text.parse::<usize>().is_ok() {
                write!(sheet, "<c r=\"{cell}\"><v>{text}</v></c>").unwrap();
            } else {
                write!(
                    sheet,
                    "<c r=\"{cell}\" t=\"inlineStr\"><is><t xml:space=\"preserve\">{}</t></is></c>",
                    crate::esc(text)
                )
                .unwrap();
            }
        }
        sheet.push_str("</row>");
    }
    write!(
        sheet,
        "</sheetData><autoFilter ref=\"A1:{}{}\"/></worksheet>",
        column(headers.len()),
        rows.len() + 1
    )
    .unwrap();
    let book = format!(
        r#"<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{}" sheetId="1" r:id="rId1"/></sheets></workbook>"#,
        crate::esc(title)
    );
    archive(&[
        ("[Content_Types].xml", TYPES),
        ("_rels/.rels", ROOT_REL),
        ("xl/workbook.xml", &book),
        ("xl/_rels/workbook.xml.rels", BOOK_REL),
        ("xl/worksheets/sheet1.xml", &sheet),
    ])
}
pub(super) fn link(title: &str, headers: &[&str], rows: &[Vec<String>]) -> String {
    let data = base64(&workbook(title, headers, rows));
    format!(
        r#"<a class="btn" download="{}.xlsx" href="data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,{data}">Скачать Excel</a>"#,
        crate::esc(title)
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn package_primitives() {
        assert_eq!(crc32(b"123456789"), 0xcbf43926);
        assert_eq!(base64(b"x"), "eA==");
        assert_eq!(base64(b"xy"), "eHk=");
        assert_eq!(column(81), "CC");
    }
}
