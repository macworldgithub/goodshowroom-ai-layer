import type { ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";

/**
 * Restricted Markdown for the API's `answer` text.
 *
 * - Raw HTML is dropped (`skipHtml`), and MDX is not enabled, so `{...}` is plain text.
 * - Only the elements below can appear. Images, iframes, scripts and forms cannot.
 * - Only absolute `https:` links are kept. Other links show as plain text.
 * - No plugin and no `dangerouslySetInnerHTML` is used.
 */
const ALLOWED = ["p", "strong", "em", "ul", "ol", "li", "code", "pre", "blockquote", "br", "a", "h1", "h2", "h3", "h4", "h5", "h6"];

/** Return the link only when it is an absolute https URL without credentials. */
export function safeUrl(value: string): string | null {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || !url.hostname) return null;
    return url.href;
  } catch {
    return null;
  }
}

function Heading({ children }: { children?: ReactNode }) {
  // Page headings belong to the layout. A Markdown heading is only a bold line.
  return <p className="mt-2 font-semibold">{children}</p>;
}

const components: Components = {
  a({ href, children }) {
    if (!href) return <span>{children}</span>;
    return <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="text-blue-800 underline">{children}</a>;
  },
  h1: Heading, h2: Heading, h3: Heading, h4: Heading, h5: Heading, h6: Heading,
  code({ children }) { return <code className="rounded bg-slate-100 px-1 py-0.5 text-[0.9em]">{children}</code>; },
};

export function SafeMarkdown({ children }: { children: string }) {
  return (
    <div className="space-y-2 leading-7 [&_ol]:list-decimal [&_ol]:pl-6 [&_ul]:list-disc [&_ul]:pl-6">
      <ReactMarkdown skipHtml allowedElements={ALLOWED} unwrapDisallowed components={components} urlTransform={(url) => safeUrl(url) ?? ""}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
