import asyncio
from playwright.async_api import async_playwright

async def get_response_when_finished(page, response_selector=None, prompt_text=None):
    try:
        # Initial wait for network redirects (like /new -> /chat)
        await page.wait_for_timeout(4000)
        
        previous_text = ""
        unchanged_ticks = 0
        
        # Universal detection: monitor the ENTIRE page text to see when it stops streaming
        while True:
            await page.wait_for_timeout(1000) # Poll every second
            current_text = await page.evaluate("document.body.innerText")
            
            if current_text and current_text == previous_text:
                unchanged_ticks += 1
                if unchanged_ticks >= 4: # Global page text stopped changing for 4 seconds
                    break
            else:
                unchanged_ticks = 0
                previous_text = current_text
                
        # Now that streaming is definitively over, extract the message
        # Use our preferred selector first
        if response_selector:
            count = await page.locator(response_selector).count()
            if count > 0:
                return await page.locator(response_selector).last.inner_text()
                
        # Fallback if the selector was completely wrong or changed by developers
        # We return the last few significant lines of the page text, which is usually the chatbot's message!
        lines = [line.strip() for line in current_text.split('\n') if line.strip()]
        
        if prompt_text:
            # Find the last occurrence of the prompt in the text and cut everything before it!
            start_idx = 0
            for i in range(len(lines) - 1, -1, -1):
                # Use exact match so we don't accidentally match the AI's response 
                # (e.g., if AI replies "Hey! How can I help?", it contains "hey")
                if prompt_text.lower() == lines[i].lower():
                    start_idx = i + 1
                    break
            if start_idx > 0:
                lines = lines[start_idx:]
            else:
                lines = lines[-15:]
        else:
            lines = lines[-15:]
            
        # Clean up common Claude/Gemini UI junk at the bottom of the page
        ignore_phrases = [
            "Claude is AI", "can make mistakes", "Share",
            "Sonnet", "Copy", "Good response", "Bad response",
            "Free plan", "Greeting", "OpenClaw"
        ]
        
        clean_lines = []
        for line in lines:
            if any(phrase.lower() in line.lower() for phrase in ignore_phrases):
                continue
            # Filter out timestamps like "9:46 PM"
            if len(line) <= 8 and ("AM" in line or "PM" in line):
                continue
            clean_lines.append(line)
            
        return "\n".join(clean_lines).strip()
                
    except Exception as e:
        return f"[Error waiting for response: {e}]"

async def main():
    async with async_playwright() as p:
        # The key to keeping logins persistent is using a user_data_dir.
        # Playwright will store cookies, local storage, and session data in this folder.
        user_data_dir = "./persistent_browser_data"
        
        # We launch headful (headless=False) so you can actually perform the login!
        # Once logged in, you can choose to make it headless for background tasks.
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            channel="chrome", # Using regular Chrome installed on your machine to bypass bot detection usually works better
            args=["--disable-blink-features=AutomationControlled"] # Standard flag against bot detection
        )
        
        page = context.pages[0] if context.pages else await context.new_page()
        
        print("====== PLAYWRIGHT BOT STARTING ======")
        print("If you aren't logged in, the script will wait, giving you a chance to do so.")
        print("If you DO log in, the session will be saved for next time!\n")
        
        # --- TEST CLAUDE ---
        try:
            print("Navigating to Claude...")
            await page.goto("https://claude.ai/new", wait_until="domcontentloaded")
            await page.wait_for_timeout(5000) # Initial sleep to let site verify/load
            
            # Simple check if there's a chat box (Claude uses contenteditable elements for their inputs)
            chat_box = page.locator("div[contenteditable='true']")
            
            if await chat_box.count() > 0:
                print("Logged in! Found Claude chat box.")
                await chat_box.first.fill("hey")
                await page.keyboard.press("Enter")
                print("Sent 'hey' to Claude. Waiting for response to finish...")
                
                # We broadened the selector here since Claude recently updated its UI classes
                response = await get_response_when_finished(page, ".font-claude-message, .prose, [data-message-author='assistant']", prompt_text="hey")
                print(f"\n--- Claude's Reply ---\n{response}\n----------------------\n")
            else:
                print("Could not find Claude chat box. You likely need to log in.")
                print("Waiting 60 seconds... Please log in to Claude now so it saves the session!")
                await page.wait_for_timeout(60000)
        except Exception as e:
            print(f"Error with Claude interaction: {e}")
            
        print("\n-------------------------------------------------\n")

        # --- TEST GEMINI ---
        try:
            print("Navigating to Gemini...")
            await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            
            # Gemini typically uses contenteditable inside a rich-textarea
            # More generically, we can try to find the standard input area.
            chat_box = page.locator("rich-textarea div[contenteditable='true']").first
            
            # Sometimes they adjust UI, so we check if the element exists
            if await chat_box.count() > 0:
                print("Logged in! Found Gemini chat box.")
                await chat_box.fill("hey")
                await page.keyboard.press("Enter")
                print("Sent 'hey' to Gemini. Waiting for response to finish...")
                
                response = await get_response_when_finished(page, "message-content", prompt_text="hey")
                print(f"\n--- Gemini's Reply ---\n{response}\n----------------------\n")
            else:
                print("Could not find Gemini chat box. You likely need to log in.")
                print("Waiting 60 seconds... Please log in to Google/Gemini now so it saves the session!")
                await page.wait_for_timeout(60000)
        except Exception as e:
            print(f"Error with Gemini interaction: {e}")

        
        print("\nTest completed. Closing browser.")
        await context.close()

if __name__ == "__main__":
    asyncio.run(main())
