import { formatEmailReading } from "./formatEmailReading";

type EmailReadingProps = {
  text: string;
};

export default function EmailReading({ text }: EmailReadingProps) {
  const blocks = formatEmailReading(text);
  if (!blocks.length) {
    return <p className="mail-reading-empty">No email content available</p>;
  }

  return (
    <div className="mail-reading">
      {blocks.map((block, index) => {
        if (block.kind === "kicker") {
          return (
            <p key={index} className="mail-reading-kicker">
              {block.text}
            </p>
          );
        }
        if (block.kind === "headline") {
          return (
            <h4 key={index} className="mail-reading-headline">
              {block.text}
            </h4>
          );
        }
        if (block.kind === "tag") {
          return (
            <p key={index} className="mail-reading-tag">
              {block.text}
            </p>
          );
        }
        if (block.kind === "section") {
          return (
            <section key={index} className="mail-reading-section">
              <h5>{block.title}</h5>
              <p>{block.text}</p>
            </section>
          );
        }
        if (block.kind === "details") {
          return (
            <dl key={index} className="mail-reading-details">
              {block.rows.map((row) => (
                <div key={row.label}>
                  <dt>{row.label}</dt>
                  <dd>{row.value}</dd>
                </div>
              ))}
            </dl>
          );
        }
        return (
          <p key={index} className="mail-reading-paragraph">
            {block.text}
          </p>
        );
      })}
    </div>
  );
}
