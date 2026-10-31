import { FileText } from 'lucide-react'
import type { Citation } from '../../api/assistant'

const SOURCES_BLOCK = /\n{2}Sources:\n[\s\S]*$/

/**
 * The answer text without its trailing plain-text "Sources:" list.
 *
 * The server appends that list so text-only clients and saved history still show
 * the sources. When the structured citations are available the list below is
 * shown instead, so the same sources are not shown twice.
 */
export function withoutSourcesBlock(content: string): string {
  return content.replace(SOURCES_BLOCK, '')
}

interface CitationListProps {
  citations: Citation[]
}

/** Where each [n] in an answer comes from: document, page / section, and the passage. */
export function CitationList({ citations }: CitationListProps) {
  if (citations.length === 0) return null
  return (
    <section className="assistant-citations" aria-label="Sources" data-testid="assistant-citations">
      <h4 className="assistant-citations-title">
        <FileText size={13} aria-hidden="true" /> Sources
      </h4>
      <ol className="assistant-citations-list">
        {citations.map((citation) => (
          <li key={citation.id} className="assistant-citation">
            <span className="assistant-citation-id">[{citation.id}]</span>
            <div className="assistant-citation-body">
              <span className="assistant-citation-doc">{citation.source}</span>
              <span className="assistant-citation-where">
                {[citation.page, citation.section && `Section: ${citation.section}`]
                  .filter(Boolean)
                  .join(' · ') || citation.label.replace(`${citation.source}, `, '')}
              </span>
              {citation.quote && <q className="assistant-citation-quote">{citation.quote}</q>}
            </div>
          </li>
        ))}
      </ol>
    </section>
  )
}
