from google import genai
import appsettings

client = genai.Client(api_key=appsettings.GEMINI_API_KEY)

response = client.models.generate_content(
    model="gemini-3.6-flash",
    contents=f"Da li za adresu {appsettings.STREET_ADDRESS} ima danas iskljucenja struje ili vode?",
)

print(response.text)
