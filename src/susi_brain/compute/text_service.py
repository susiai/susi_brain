import requests
from fastapi import Body, Request

class ChatCompletion:

    # call i.e.:
    # curl http://localhost:8042/api/v1/compute/chat/completions -H "Content-Type: application/json" -d '{"model": "gpt-3.5-turbo","messages": [{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": "Hello!"}]}'
    def post(self, request: Request, data: dict | None = Body(default=None)):
        openai_api_key = request.app.state.settings.openai_api_key.get_secret_value()
        if not data:
            return {'error': 'No data provided'}

        messages = data.get('messages')
        if not messages:
            return {'error': 'No messages provided'}

        if not openai_api_key:
            return {'error': 'No OpenAI API key provided'}

        headers = {
            'Authorization': f'Bearer {openai_api_key}',
            'Content-Type': 'application/json'
        }
        data = {
            'messages': messages,
            'model': data.get('model', 'gpt-3.5-turbo'),
            'temperature': data.get('temperature', 1.0),
            'max_tokens': data.get('max_tokens', 50)
        }

        response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=data)
        return response.json()
