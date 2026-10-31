// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it } from 'vitest'
import type { Citation } from '../../api/assistant'
import { CitationList, withoutSourcesBlock } from './CitationList'

const remote: Citation = {
  id: 1,
  document_id: 'doc-1',
  document_version: 1,
  source: '1_HR_Remote_Work_Policy.pdf',
  page: 'Page 2',
  section: 'Remote Work',
  label: '1_HR_Remote_Work_Policy.pdf, Page 2, section "Remote Work"',
  quote: 'Employees may work remotely for up to 15 days per year.',
}

afterEach(cleanup)

describe('CitationList', () => {
  it('shows the document, page, section and the supporting passage', () => {
    render(<CitationList citations={[remote]} />)

    const sources = screen.getByRole('region', { name: 'Sources' })
    expect(within(sources).getByText('[1]')).toBeInTheDocument()
    expect(within(sources).getByText('1_HR_Remote_Work_Policy.pdf')).toBeInTheDocument()
    expect(within(sources).getByText('Page 2 · Section: Remote Work')).toBeInTheDocument()
    expect(within(sources).getByText(/up to 15 days per year/)).toBeInTheDocument()
  })

  it('falls back to the label when a source has neither page nor section', () => {
    render(
      <CitationList
        citations={[
          { ...remote, source: 'Policy.pdf', page: null, section: null, label: 'Policy.pdf, passage 4' },
        ]}
      />,
    )

    expect(screen.getByText('passage 4')).toBeInTheDocument()
  })

  it('renders a quote as plain text, never as markup', () => {
    render(<CitationList citations={[{ ...remote, quote: '<img src=x onerror=alert(1)> Fact.' }]} />)

    expect(screen.getByText(/Fact\./)).toBeInTheDocument()
    expect(document.querySelector('img')).toBeNull()
  })

  it('renders nothing without citations', () => {
    const { container } = render(<CitationList citations={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('removes the plain-text Sources list once structured citations are shown', () => {
    const text = 'Up to 15 days per year. [1]\n\nSources:\n[1] 1_HR_Remote_Work_Policy.pdf, Page 2'

    expect(withoutSourcesBlock(text)).toBe('Up to 15 days per year. [1]')
    expect(withoutSourcesBlock('No sources here.')).toBe('No sources here.')
  })
})
