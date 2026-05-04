
```python
import os
from groq import Groq

class AlphaIntelligence:
    def __init__(self, historical_data):
        self.data = historical_data
        # Inizializza il client Cloud Groq
        self.client = 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

    def get_institutional_verdict(self):
        try:
            prompt = f"""
            Agisci come un analista quantitativo istituzionale.
            Ecco gli ultimi dati di chiusura dell'asset:
            {self.data['Close'].tail(5).to_string()}
            
            Analizza il trend. Rispondi SOLO con una di queste parole esatte:
            STRONG_BUY, BUY, NEUTRAL, SELL, SELL/SWAP_TO_GOLD.
            Non aggiungere spiegazioni.
            """
            
            chat_completion = self.client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama3-70b-8192", # Modello potentissimo di Meta
                temperature=0.1,
                max_tokens=10
            )
            
            response = chat_completion.choices[0].message.content.strip().upper()
            
            valid_responses = ["STRONG_BUY", "BUY", "NEUTRAL", "SELL", "SELL/SWAP_TO_GOLD"]
            for r in valid_responses:
                if r in response:
                    return r
            return "NEUTRAL"
        except Exception as e:
            print(f"Errore Groq Verdict: {e}")
  
...
