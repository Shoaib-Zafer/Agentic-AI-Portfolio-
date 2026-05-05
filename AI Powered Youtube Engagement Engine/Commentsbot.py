# Import the function to create a connection to Google's API
from googleapiclient.discovery import build
# Import the tool to handle logging into a Google account via browser/console
from google_auth_oauthlib.flow import InstalledAppFlow
# Import the error type for when Google API requests fail
from googleapiclient.errors import HttpError
# Import the library to talk to the local AI model (Ollama)
import ollama
# Import a tool to inspect functions and see what arguments they need
import inspect

# Import json module to read and write data files
import json
# Import time module to allow the script to pause (sleep)
import time
# Import regex module for parsing responses
import re
import socket
# Import os module to read environment variables off your computer
import os
# Import logging module to print messages to the screen safely
import logging

# Configuration - set these before running
# Grab the YouTube API key from system environment variables (optional)
API_KEY = os.environ.get("YOUTUBE_API_KEY", "")  # optional, used for read-only if OAuth not provided
# Grab the Channel ID from system environment variables (optional)
CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "")  # optional if using OAuth (will be discovered)
# The filename that has your Google OAuth secret keys (needed to reply)
CLIENT_SECRETS_FILE = "client_secrets.json"  # required to post comments
# The specific YouTube permission we want to ask the user for
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# Ollama settings (model name as configured in your local Ollama install)
# Get the model name to use from environment variables, or default to "llama2"
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama2")
# The file where we will save all comment IDs we've already replied to
REPLIED_STORE = "replied_comments.json"
# The amount of time to wait before checking for new comments again (10 minutes)
POLL_INTERVAL_SECONDS = 10 * 60  # 10 minutes

# Configure logging to show "INFO" level messages and above
logging.basicConfig(level=logging.INFO)
# Create a logger object we can use to print out updates
logger = logging.getLogger("commentsbot")


def load_replied_set(path):
    # Try block catches errors in case the file doesn't exist yet
    try:
        # Open the file in read mode ('r') with UTF-8 encoding
        with open(path, "r", encoding="utf-8") as f:
            # Read the JSON text in the file and convert it to a Python list
            data = json.load(f)
            # Convert the list into a Python 'set' (makes checking if an ID exists much faster)
            return set(data)
    except Exception:
        # If anything goes wrong (like file not found), return an empty set
        return set()


def save_replied_set(path, s):
    # Open the file in write mode ('w'), which will overwrite existing data
    with open(path, "w", encoding="utf-8") as f:
        # Convert our set back into a list, and save it as formatted JSON
        json.dump(list(s), f, ensure_ascii=False, indent=2)


def get_authenticated_service():
    """
    If CLIENT_SECRETS_FILE exists, run OAuth flow and return youtube service and the authenticated channel id.
    Otherwise, return a read-only youtube service using API_KEY (posting will be disabled).
    This function prefers an interactive console flow if available; if not, it falls back to the local-server flow.
    """
    # Check if the secret credentials file is in the folder
    socket.setdefaulttimeout(30)
    if os.path.exists(CLIENT_SECRETS_FILE):
        # Create an authentication flow using those credentials and our requested permissions
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)

        # Some google-auth-oauthlib versions provide run_console(), others provide run_local_server().
        # Try run_console first (text-based), otherwise fall back to run_local_server().
        creds = None
        # Check if the flow tool supports console-based login
        if hasattr(flow, "run_console"):
            logger.info("Using OAuth flow.run_console()")
            # Start the console login prompt and wait for the user to complete it
            creds = flow.run_console()
        # If console login isn't available, check for local server (opens web browser)
        elif hasattr(flow, "run_local_server"):
            logger.info("run_console() not available; using flow.run_local_server()")
            # run_local_server opens a browser by default. port=0 picks an available port.
            creds = flow.run_local_server(port=0)
        else:
            # Throw an error if neither login method exists
            raise RuntimeError("InstalledAppFlow does not expose run_console or run_local_server on this environment.")

        # Build our YouTube client object using the new login credentials
        youtube = build("youtube", "v3", credentials=creds)
        youtube._http.timeout = 30
        # get own channel id by asking the API for "mine"
        resp = youtube.channels().list(part="id,snippet", mine=True).execute()
        # Extract the items array from the API response
        items = resp.get("items", [])
        # If there are no items, this Google account doesn't have a YouTube channel
        if not items:
            raise RuntimeError("Authenticated but no channel found for the authorized account.")
        # Grab the channel ID from the first item
        channel_id = items[0]["id"]
        # Log who we logged in as
        logger.info("Authenticated as channel id: %s", channel_id)
        # Return the client, the channel ID, and True (meaning we are allowed to post comments)
        return youtube, channel_id, True
    else:
        # If the secrets file doesn't exist, check if an API key is set
        if not API_KEY:
            # If there's neither a secret file nor an API key, we can't do anything
            raise RuntimeError("No CLIENT_SECRETS_FILE and no API_KEY provided. Provide one of them.")
        # Build a read-only YouTube client using just the API key
        youtube = build("youtube", "v3", developerKey=API_KEY)
        youtube._http.timeout = 30
        # Make sure the user told us which channel to look at
        if CHANNEL_ID:
            # Return the client, the target channel ID, and False (meaning we cannot post comments)
            return youtube, CHANNEL_ID, False
        else:
            # If using just an API key, we have no way to auto-detect the channel, so throw an error
            raise RuntimeError("When using API_KEY only you must set CHANNEL_ID environment variable.")


def get_uploads_playlist_id(youtube, channel_id):
    # Ask the API for the "contentDetails" of the given channel ID
    resp = youtube.channels().list(part="contentDetails", id=channel_id).execute()
    # Extract the results item list
    items = resp.get("items", [])
    # If the channel isn't found, throw an error
    if not items:
        raise RuntimeError("Channel not found: " + channel_id)
    # Dig into the data structure to find the special hidden playlist that holds all uploaded videos
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def list_all_video_ids(youtube, uploads_playlist_id):
    # Create an empty list to store our video IDs in
    video_ids = []
    # Create a variable to hold our "page token" (for skipping to the next page of results)
    page_token = None
    # Run an infinite loop that breaks when we finish checking all pages
    while True:
        # Ask the API for items in the "uploads" playlist
        resp = youtube.playlistItems().list(
            part="snippet",
            playlistId=uploads_playlist_id,
            maxResults=50, # Get up to 50 videos at a time
            pageToken=page_token # Tell the API which page we're on
        ).execute()
        # Loop through each item (video) returned by the API
        for it in resp.get("items", []):
            # Extract the actual video ID from the complex data dictionary
            vid = it["snippet"]["resourceId"]["videoId"]
            # Add the video ID to our list
            video_ids.append(vid)
        # Try to get the token for the next page of results
        page_token = resp.get("nextPageToken")
        # If there is no next page token, it means we reached the end
        if not page_token:
            # Break out of the infinite while loop
            break
    # Return the full list of video IDs we collected
    return video_ids


def collect_unreplied_comments(youtube, video_id, my_channel_id, replied_set):
    """
    Returns list of (comment_id, author_display_name, comment_text)
    A comment is considered unreplied if:
      - totalReplyCount == 0
      - AND comment_id not in replied_set
      - OR there is no reply by my_channel_id in existing replies
    """
    # Create an empty list for comments we need to reply to
    results = []
    # Create a token for pagination (flipping through pages of comments)
    page_token = None
    # Keep looping to look through all pages of comments
    while True:
        # Ask the YouTube API to fetch comment threads for a specific video
        logger.info("Fetching comments for video %s (page token: %s)", video_id, page_token or "none")
        fetch_start = time.perf_counter()
        try:
            resp = youtube.commentThreads().list(
                part="snippet,replies",
                videoId=video_id,
                maxResults=100, # Max number of top-level comments to get per page
                pageToken=page_token,
                textFormat="plainText" # Tell YouTube we just want plain text, no HTML
            ).execute()
        except HttpError as e:
            logger.error("Failed to fetch comments for video %s: %s", video_id, e)
            break
        except (socket.timeout, TimeoutError) as e:
            logger.error("Timed out fetching comments for video %s: %s", video_id, e)
            break
        finally:
            fetch_end = time.perf_counter()
            logger.info("Fetch duration for video %s (page token: %s): %.2f seconds", video_id, page_token or "none", fetch_end - fetch_start)
        # Loop over every single comment thread the API gave us
        for thread in resp.get("items", []):
            # Get the top comment (the main one that started the thread)
            top = thread["snippet"]["topLevelComment"]
            # Extract the unique ID of the comment
            comment_id = top["id"]
            # Extract the details dictionary of the comment
            snippet = top["snippet"]
            # Extract the actual text the user typed
            text = snippet.get("textOriginal", "")
            # Extract the username of the person who commented
            author = snippet.get("authorDisplayName", "")
            # Count how many replies this comment already has
            total_replies = thread["snippet"].get("totalReplyCount", 0)

            # If we've already replied to this comment before (checked against our list), skip it
            if comment_id in replied_set:
                # 'continue' jumps straight to the next loop iteration (the next comment)
                continue

            # If there are replies, check if any reply exists (skip all replied threads)
            if total_replies > 0:
                # mark as replied in our set so we avoid future processing
                replied_set.add(comment_id)
                # jump to the next comment
                continue

            # If no replies or no reply by us, then it's a candidate for the bot to answer
            results.append((comment_id, author, text))
        # Look for the next page token
        page_token = resp.get("nextPageToken")
        # If there isn't one, we're done scrolling through the comments
        if not page_token:
            break
    # Return all the comments that need a reply
    return results


def _clean_reply_text(text):
    if text is None:
        return ""
    cleaned = str(text).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ("\"", "'"):
        cleaned = cleaned[1:-1].strip()
    return cleaned


def _parse_ollama_response(resp):
    """
    Best-effort extraction from common ollama Python client response shapes.
    """
    # If the AI gave absolutely nothing back, return an empty string
    if resp is None:
        return ""
    # If resp is dict-like (formatted as key-value pairs)
    try:
        # Check if the response is a dictionary
        if isinstance(resp, dict):
            # Some client versions return {'choices':[{'text':...}]} format
            if "choices" in resp and isinstance(resp["choices"], list) and resp["choices"]:
                # Grab the first choice
                choice = resp["choices"][0]
                if isinstance(choice, dict):
                    # Try to pull the text out of the choice object safely
                    return _clean_reply_text(choice.get("text") or (choice.get("message") and choice["message"].get("content")) or "")
            if "response" in resp:
                return _clean_reply_text(resp.get("response", ""))
            # other shapes might have a direct 'generated_text' property
            if "generated_text" in resp:
                # Pluck out that generated text
                return _clean_reply_text(resp.get("generated_text", ""))
            # other shapes might have an 'output' list
            if "output" in resp and isinstance(resp["output"], list):
                # Build an array to piece the text together
                parts = []
                for o in resp["output"]:
                    if isinstance(o, dict):
                        parts.append(o.get("text") or o.get("content") or "")
                    else:
                        parts.append(str(o))
                # Join all text pieces and clear surrounding whitespace
                return _clean_reply_text(" ".join([p for p in parts if p]).strip())
        # If object acts like a class with a .text attribute
        if hasattr(resp, "text"):
            return _clean_reply_text(getattr(resp, "text"))
        # If object acts like a class with a .content attribute
        if hasattr(resp, "content"):
            return _clean_reply_text(getattr(resp, "content"))
        if hasattr(resp, "response"):
            return _clean_reply_text(getattr(resp, "response"))
    except Exception:
        # If any step fails, ignore the error and fall to fallback method
        pass
    # fallback to standard string conversion as a last resort
    try:
        # Try to convert whatever the object is to a normal string
        as_text = str(resp).strip()
        match = re.search(r"response=([\"'])(.*?)\1", as_text, re.DOTALL)
        if match:
            return _clean_reply_text(match.group(2))
        return _clean_reply_text(as_text)
    except Exception:
        # If string conversion crashes, return an empty string safely
        return ""


def _safe_generate_call(func, model, prompt, **desired_kwargs):
    """
    Call `func` (ollama generate) while adapting to the function's accepted parameters.
    Filters desired_kwargs to only those parameters present in the callable signature,
    and tries positional fallback if necessary.
    """
    # Create empty blueprint variable
    sig = None
    try:
        # Check what arguments the AI generation function technically accepts
        sig = inspect.signature(func)
        # Create a set of those valid argument names
        accepted = set(sig.parameters.keys())
    except Exception:
        # If we can't inspect it, assume it accepts no extra arguments
        accepted = set()

    # filter kwargs (extra settings) to only keep ones the function actually accepts
    call_kwargs = {k: v for k, v in desired_kwargs.items() if k in accepted}
    # Try calling the AI function using exact parameter names first
    try:
        return func(model=model, prompt=prompt, **call_kwargs)
    except TypeError:
        # Try positional fallback (where model and prompt aren't explicitly named)
        try:
            return func(model, prompt, **call_kwargs)
        except TypeError as e:
            # Try minimal call with absolutely purely just the model and prompt text
            try:
                return func(model=model, prompt=prompt)
            except Exception:
                # re-raise the last error if nothing works so it can be logged above
                raise e


def generate_reply(comment_text):
    """
    Use the installed ollama Python package to generate a short, persona-based reply.
    Expects the Python package 'ollama' to be installed via pip and a local model with name OLLAMA_MODEL.
    This version adapts to different installed ollama API signatures (filters unsupported kwargs).
    """
    # Create the exact instructions to feed the AI, wrapping the user's comment
    prompt = (
        "You are a friendly, positive 14-year-old. Reply briefly (one or two short sentences) "
        "to the YouTube comment below in a warm, upbeat, and age-appropriate tone. Do not ask for personal info.\n\n"
        f"Comment: \"{comment_text}\"\n\nReply:"
    )

    # Desired kwargs (extra settings to tweak AI output: higher temp is more creative, max_tokens prevents huge essays)
    desired = {"temperature": 0.8, "max_tokens": 128}

    try:
        # Try finding the correct method in the library to trigger the AI
        if hasattr(ollama, "generate"):
            # Call standard generic 'generate' method
            resp = _safe_generate_call(ollama.generate, OLLAMA_MODEL, prompt, **desired)
        elif hasattr(ollama, "Ollama") and hasattr(ollama.Ollama, "generate"):
            # Init a client object if library requires objects, then call
            client = ollama.Ollama()
            resp = _safe_generate_call(client.generate, OLLAMA_MODEL, prompt, **desired)
        elif hasattr(ollama, "run"):
            # check if library exposes a 'run' method instead of generate
            resp = _safe_generate_call(ollama.run, OLLAMA_MODEL, prompt, **desired)
        else:
            # Throw error if we have no clear way to call the AI
            raise RuntimeError("Installed 'ollama' package does not expose a supported generate API.")
    except Exception as e:
        # Log crashes relating to local AI generation
        logger.error("Ollama generation failed: %s", e)
        return ""

    # Parse and clean whatever mess the AI gave back to us
    return _parse_ollama_response(resp)


def post_reply(youtube, parent_comment_id, text):
    # Assemble the data payload YouTube expects to create a reply
    body = {
        "snippet": {
            # Attach the new comment directly to the parent's ID
            "parentId": parent_comment_id,
            # Provide the raw string text we want to post
            "textOriginal": text
        }
    }
    # Tell the YouTube API to insert this comment
    resp = youtube.comments().insert(part="snippet", body=body).execute()
    # Return the response block, guaranteeing it was sent successfully
    return resp


def main():
    # Load previously replied IDs from the JSON file into memory
    replied = load_replied_set(REPLIED_STORE)
    # Log in to YouTube and check whether the developer allowed posting
    youtube, my_channel_id, can_post = get_authenticated_service()
    # Log posting status
    logger.info("Posting enabled: %s", can_post)
    # Grab the secret uploads playlist ID to get a list of all your videos
    uploads_playlist = get_uploads_playlist_id(youtube, my_channel_id)
    # Log playlist ID status
    logger.info("Uploads playlist id: %s", uploads_playlist)

    # Sanity-check ollama availability
    try:
        logger.info("ollama module imported; attempting a lightweight model check.")
        # many ollama clients expose a list or models endpoint; try best-effort
        if hasattr(ollama, "models"):
            try:
                # Test connectivity to the AI by asking what models exist on your PC
                models = ollama.models()
                logger.info("ollama reports available models: %s", models)
            except Exception:
                # Disregard failures here, as it's only a connection check
                pass
    except Exception:
        # Print a warning if AI library seems completely broken
        logger.warning("ollama module not usable from Python; generation will fail until resolved.")

    # Endless loop so the script runs 24/7 forever
    while True:
        try:
            # Query YouTube for all your videos
            video_ids = list_all_video_ids(youtube, uploads_playlist)
            # Log exact count of how many videos are on the channel
            logger.info("Found %d videos", len(video_ids))
            # Start cycling through every video one by one
            for vid in video_ids:
                try:
                    # Find out which comments on THIS video need a reply
                    candidates = collect_unreplied_comments(youtube, vid, my_channel_id, replied)
                    # Log the count of comments needing attention
                    logger.info("Video %s: %d unreplied comments", vid, len(candidates))
                    
                    # Cycle through each raw, unreplied comment on this video
                    for comment_id, author, text in candidates:
                        try:
                            # Log whose comment we're analyzing right now
                            logger.info("Generating reply for comment %s by %s", comment_id, author)
                            
                            # Give the text to the AI model and ask it for a reply
                            reply_text = generate_reply(text)
                            
                            # If AI returns nothing, pre-mark it as 'replied' anyway so we don't get stuck doing it forever
                            if not reply_text:
                                logger.warning("ollama returned empty reply for comment %s; skipping", comment_id)
                                replied.add(comment_id)
                                continue

                            # Show the original comment and proposed reply
                            logger.info("Original comment by %s: %s", author, text)
                            logger.info("Generated reply: %s", reply_text)

                            # Ask for approval before posting
                            approval = input("Post this reply? (y/n): ").strip().lower()
                            if approval not in {"y", "yes"}:
                                logger.info("Skipped reply for comment %s", comment_id)
                                replied.add(comment_id)
                                save_replied_set(REPLIED_STORE, replied)
                                continue

                            # If API keys are set up correctly & OAuth works
                            if can_post:
                                try:
                                    # Actually deliver the reply to YouTube's servers
                                    post_resp = post_reply(youtube, comment_id, reply_text)
                                    # Log the newly created Reply ID on YouTube!
                                    logger.info("Posted reply id: %s", post_resp.get("id"))
                                except HttpError as e:
                                    # Error handling specifically for Google connection issues
                                    logger.error("Failed to post reply for %s: %s", comment_id, e)
                                    # do not add to replied set in case we want to retry later
                                    continue
                            else:
                                logger.warning("Posting disabled; reply approved but not posted.")

                            # Keep a record that we handled this successfully
                            replied.add(comment_id)
                            # Save the new array to disk so if the script reboots, it remembers
                            save_replied_set(REPLIED_STORE, replied)
                        except Exception as e:
                            # Print any generalized crash handling this comment
                            logger.exception("Error handling comment %s: %s", comment_id, e)
                except Exception as e:
                    # Print any generalized crash handling this video chunk
                    logger.exception("Error scanning video %s: %s", vid, e)

        except Exception as e:
            # Print any gigantic crash with fetching the video list/playlist completely
            logger.exception("Main loop error: %s", e)

        # Notify the operator it is going to rest
        logger.info("Sleeping for %d seconds...", POLL_INTERVAL_SECONDS)
        # Block the script from executing the next loop for 10 minutes to save API quota
        time.sleep(POLL_INTERVAL_SECONDS)

# When you run `python Commentsbot.py` in the terminal, __name__ matches "__main__"
if __name__ == "__main__":
    # Thus, it triggers the engine start
    main()