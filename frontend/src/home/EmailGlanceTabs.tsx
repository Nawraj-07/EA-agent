import {
  derivePriorityTag,
  deriveTopicTags,
  type GlanceEmail,
  type TopicTag,
} from "./emailTags";

type EmailGlanceTabsProps = {
  email: GlanceEmail;
  extraTopics?: TopicTag[];
  className?: string;
};

export default function EmailGlanceTabs({
  email,
  extraTopics = [],
  className = "",
}: EmailGlanceTabsProps) {
  const topics = [
    ...new Set([...deriveTopicTags(email), ...extraTopics]),
  ].slice(0, 2);
  const priority = derivePriorityTag(email);
  if (!topics.length && !priority) return null;

  return (
    <span
      className={`email-glance-tabs${className ? ` ${className}` : ""}`}
      aria-label="Email labels"
    >
      {topics.map((tag) => (
        <span key={tag} className="email-glance-tab topic">
          {tag}
        </span>
      ))}
      {priority ? (
        <span className={`email-glance-tab priority ${priority}`}>
          {priority}
        </span>
      ) : null}
    </span>
  );
}
