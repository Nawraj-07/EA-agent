export type EmailReadingBlock =
  | { kind: "kicker"; text: string }
  | { kind: "headline"; text: string }
  | { kind: "tag"; text: string }
  | { kind: "section"; title: string; text: string }
  | { kind: "details"; rows: { label: string; value: string }[] }
  | { kind: "paragraph"; text: string };

const DROP_LINE =
  /^(untitled document|read more|need help\b.*|click here\b.*)$/i;

const FOOTER_LINE =
  /newsletter preferences|to unsubscribe|manage your preferences/i;

const SECTION_LINE =
  /^(summary|headnote|key details|facts|held)\s*:\s*(.*)$/i;

function cleanLines(raw: string): string[] {
  return raw
    .replace(/\u00a0/g, " ")
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => line.replace(/[ \t]+/g, " ").trim())
    .filter((line) => line && !DROP_LINE.test(line) && !FOOTER_LINE.test(line));
}

function titleCase(value: string): string {
  return value
    .toLowerCase()
    .replace(/\b[a-z]/g, (letter) => letter.toUpperCase());
}

function parseDetail(line: string): { label: string; value: string } | null {
  const match = line.match(/^([^:]{2,36})\s*:\s+(\S.*)$/);
  if (!match) return null;
  const label = match[1].trim();
  if (label.split(" ").length > 6) return null;
  return { label, value: match[2].trim() };
}

function introBlocks(lines: string[]): EmailReadingBlock[] {
  const blocks: EmailReadingBlock[] = [];
  const kickers: string[] = [];
  let headline = "";
  let tag = "";

  for (const line of lines) {
    if (!headline && line.length >= 70) {
      headline = line;
      continue;
    }
    if (/\s\|\s/.test(line) && line.length < 90) {
      tag = line;
      continue;
    }
    if (!headline) {
      kickers.push(line);
      continue;
    }
    blocks.push({ kind: "paragraph", text: line });
  }

  const ordered: EmailReadingBlock[] = [];
  if (kickers.length) ordered.push({ kind: "kicker", text: kickers.join(" · ") });
  if (headline) ordered.push({ kind: "headline", text: headline });
  if (tag) ordered.push({ kind: "tag", text: tag });
  return ordered.concat(blocks);
}

export function formatEmailReading(raw: string | null | undefined): EmailReadingBlock[] {
  const lines = cleanLines(raw || "");
  if (!lines.length) return [];

  const sectionIndexes = lines
    .map((line, index) => (SECTION_LINE.test(line) ? index : -1))
    .filter((index) => index >= 0);

  if (!sectionIndexes.length) {
    return lines.map((text) => ({ kind: "paragraph" as const, text }));
  }

  const blocks = introBlocks(lines.slice(0, sectionIndexes[0]));

  sectionIndexes.forEach((start, position) => {
    const end = sectionIndexes[position + 1] ?? lines.length;
    const match = lines[start].match(SECTION_LINE);
    if (!match) return;
    const title = titleCase(match[1]);
    const bodyLines = [match[2], ...lines.slice(start + 1, end)]
      .map((line) => line.trim())
      .filter(Boolean);

    if (/key details/i.test(match[1])) {
      const rows = bodyLines
        .map(parseDetail)
        .filter((row): row is { label: string; value: string } => row !== null);
      const prose = bodyLines.filter((line) => !parseDetail(line));
      if (rows.length) blocks.push({ kind: "details", rows });
      prose.forEach((text) => blocks.push({ kind: "paragraph", text }));
      return;
    }

    if (bodyLines.length) {
      blocks.push({ kind: "section", title, text: bodyLines.join(" ") });
    }
  });

  return blocks;
}
