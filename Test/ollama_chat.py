import requests
import json

OLLAMA_API_URL = "http://localhost:11434/api/chat"
MODEL = "gemma4:e4b"

def chat():
    print(f"Chatbot started with model: {MODEL}")
    print("Type 'exit' or 'quit' to stop.")
    
    messages = [
        {"role": "system", "content": "You are a helpful AI assistant."}
    ]
    
    # 🚀 Warm-up request: Preloads model into VRAM so your first message doesn't lag
    print("Warming up VRAM (skipping cold start)...", end="", flush=True)
    try:
        requests.post(OLLAMA_API_URL, json={"model": MODEL, "messages": [{"role": "user", "content": ""}], "keep_alive": -1, "stream": False}, timeout=5)
        print(" Done ⚡")
    except:
        print(" Failed")
    
    while True:
        try:
            user_input = input("\nYou: ")
            if user_input.lower() in ['exit', 'quit']:
                break
                
            messages.append({"role": "user", "content": user_input})
            
            payload = {
                "model": MODEL,
                "messages": messages,
                "stream": False,
                "think": False,  # Native Ollama parameter to completely skip reasoning
                "keep_alive": -1, # Pass as integer to avoid "missing unit" duration error
                "options": {
                    "num_ctx": 4096, # Restrict the context window (smaller context = significantly faster generation)
                    "temperature": 0.0, # Greedy generation eliminates sampling overhead
                    "top_k": 1, # Skip calculating probability distributions across multiple tokens
                    "num_predict": 2048, # Strict limit to stop the model from rambling
                }
            }
            
            response = requests.post(OLLAMA_API_URL, json=payload, stream=True)
            if response.status_code == 200:
                print("\nBot: ", end="", flush=True)
                full_bot_message = ""
                for line in response.iter_lines():
                    if line:
                        chunk = json.loads(line.decode('utf-8'))
                        content = chunk.get("message", {}).get("content", "")
                        print(content, end="", flush=True)
                        full_bot_message += content
                print() # Print a newline when the stream finishes
                messages.append({"role": "assistant", "content": full_bot_message})
            else:
                print(f"Error: {response.status_code} - {response.text}")
                messages.pop() # remove last user msg on error so retries work cleanly
        except requests.exceptions.ConnectionError:
            print("Error: Could not connect to Ollama. Make sure Ollama is running.")
            break
        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    chat()
