**Stage 1 — Comment Scanner**
Connects to your YouTube channel via OAuth 2.0 and scans all videos for comments that haven't received a reply from the channel owner. Supports filtering by video, date range, and comment volume. Runs on a configurable schedule to catch new comments as they arrive.

**Stage 2 — Context Builder**

Before sending a comment to the LLM, the engine enriches it with context: the video title, video description, the commenter's name, any parent comments in the thread, and historical interaction data. This context window ensures the AI generates replies that are relevant to the actual content being discussed — not generic responses.

**Stage 3 — LLM Processor**

The enriched comment is passed to a Large Language Model with a carefully engineered system prompt that enforces your channel's tone, brand guidelines, and response rules. The LLM generates a reply that sounds natural, addresses the commenter's specific point, and maintains consistency across hundreds of responses.

**Stage 4 — Reply Publisher**

The generated reply is posted back to YouTube using the official Data API v3. Built-in safeguards include rate limiting to mimic human response patterns, randomized delays between replies, and batch processing to avoid triggering YouTube's spam detection systems.
