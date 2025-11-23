import wave
import io, requests, tempfile
from fastapi.security import APIKeyHeader
from fastapi import Depends, File, Form, Request, UploadFile
from requests_toolbelt.multipart.encoder import MultipartEncoder

class VoiceTranscription:

    # call i.e.:
    # curl -X POST http://localhost:8042/api/v1/compute/audio/transcriptions -H "Content-Type: multipart/form-data" -F file="@test.wav" -F model="whisper-1"
    def post(self, request: Request, file: UploadFile | None = File(default=None),
             model: str = Form(default="tiny"),
             authorization: str | None = Depends(APIKeyHeader(name="Authorization", scheme_name="BearerToken",
                 description="Enter Bearer followed by the API key.", auto_error=False))):

        # check if openai api key is present
        openai_api_key = authorization
        if openai_api_key:
            openai_api_key = openai_api_key.replace('Bearer ', '')
        else:
            openai_api_key = request.app.state.settings.openai_api_key.get_secret_value()

        # in case that openai_api_key is not present,
        # we fail over to a local whisper model.
        model_name = model
        if not openai_api_key:
            model_name = 'tiny'

        # check if file is present
        if file is None:
            return {'error': 'No file provided'}
        audio_file = file
        audio_data = audio_file.file.read()
        audio_name = audio_file.filename

        # check if model is present and valid
        result = {}

        if openai_api_key and model_name == 'whisper-1':
            # make an online transcription
            headers = {'Authorization': f'Bearer {openai_api_key}'}
            payload = MultipartEncoder(
                fields={
                    "file": (audio_name, io.BytesIO(audio_data), "audio/x-wav"),
                    "model": model_name
                }
            )
            headers["Content-Type"] = payload.content_type
            response = requests.post("https://api.openai.com/v1/audio/transcriptions", headers=headers, data=payload)
            result = response.json()
        else:
            # make an offline transcription

            # patch model name in case no api key is present
            if model_name == 'whisper-1': model_name = 'tiny'

            # check the required technology (whisper or vosk)
            if model_name == 'tiny' or model_name == 'base' or model_name == 'small' or model_name == 'medium' or model_name == 'large': 

                # Load the offline model (it is loaded directly from the internet)
                import whisper
                model = whisper.load_model(model_name)

                # Save the received audio file temporarily to disk
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as temp_audio_file:
                    temp_audio_file.write(audio_data)
                    temp_audio_file.flush()

                    # Transcribe using the offline model
                    result = model.transcribe(temp_audio_file.name)

                result = {'text': result['text']}

            if model_name == 'en-us' or model_name == 'de': 
                # use vosk transcriptions

                wf = wave.open(io.BytesIO(audio_data), "rb")
                if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getcomptype() != "NONE":
                    return {'error': 'Audio file must be WAV format mono PCM.'}

                from vosk import Model, KaldiRecognizer
                model = Model('en-us')
                rec = KaldiRecognizer(model, wf.getframerate())
                rec.SetWords(True)
                rec.SetPartialWords(True)
                
                transcript = ""

                while True:
                    data = wf.readframes(4000)
                    if len(data) == 0:
                        break
                    if rec.AcceptWaveform(data):
                        result = rec.Result()
                        transcript += result["text"]
                    else:
                        partial_result = rec.PartialResult()
                        if partial_result:
                            transcript += partial_result["partial"]

                result = {'text': transcript}
                    
        return result
