Create a podcast episode title and description from the provided VTT transcript.
Ground every choice in the transcript; do not use outside sources or invent details.
Prior published titles, when provided, are style references only. Use them to learn
the show's voice and range without copying them or importing their subject matter.
The current transcript is the sole factual source for this episode.
When references are available, the user input is a JSON object whose
`published_title_style_references` array is style-only data and whose
`current_episode_transcript` field contains the factual VTT transcript.

Title:
- Write 2–7 words.
- Use sentence case, preserving proper names and acronyms. Do not use all caps
  or capitalize every word, even when published style references do.
- Ground it in recurring transcript themes, phrases, or images.
- Prefer the episode's central tension or story over an isolated joke or memorable aside.
- Slightly edgy and memorable, but newcomer-safe.
- Humor is secondary to the episode's meaning.
- Do not include the episode number.

Description:
- Write 2–4 conversational sentences.
- Keep the tone casual, not clinical or preachy.
- Tease the main themes, stories, and funny moments without spoiling the episode.
- Use specific transcript-grounded details.
- Use `J.` for the co-host's J-name; never spell out the full name.

Return only the requested title and description fields.
