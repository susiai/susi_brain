import sys
sys.path.insert(0, './src')

import os, argparse, logging
from flask import Flask
from waitress import serve
from flask_cors import CORS
from flask_restx import Api
from text_service import api as text_ns
from audio_service import api as audio_ns, v1api as v1audio_ns
from share_service import api as share_ns
from system_service import api as system_ns

openai_api_key = ""
app = Flask(__name__)
CORS(app)

# Log the current path(s)
home_path = os.path.expanduser('~')
cache_path = os.path.realpath(os.path.join(home_path, ".cache"))
script_path = os.path.dirname(os.path.abspath(sys.argv[0]))
app_path = os.path.realpath(os.path.join(script_path, "..", "..", ".."))
data_path = os.path.realpath(os.path.join(app_path, "data"))
os.makedirs(data_path, exist_ok=True)
work_path = os.getcwd()

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
logger.debug("Current home path:    %s", home_path)
logger.debug("Current cache path:   %s", cache_path)
logger.debug("Current app path:     %s", app_path)
logger.debug("Current data path:    %s", data_path)
logger.debug("Current script path:  %s", script_path)
logger.debug("Current working path: %s", work_path)

app.config['HOME_PATH']   = home_path
app.config['CACHE_PATH']  = cache_path
app.config['APP_PATH']    = app_path
app.config['DATA_PATH']   = data_path
app.config['SCRIPT_PATH'] = script_path
app.config['WORK_PATH']   = work_path

api = Api(app, version='1.0', title='susi_api', doc='/api/docs/')
api.add_namespace(share_ns)
api.add_namespace(text_ns)
api.add_namespace(audio_ns)
api.add_namespace(v1audio_ns)
api.add_namespace(system_ns)

def check_route_conflicts(app):
    # Getting all routes and their endpoints
    routes = [(str(route), route.endpoint) for route in app.url_map.iter_rules()]

    # Check for duplicates
    routes_str = [route[0] for route in routes]
    if len(routes_str) != len(set(routes_str)):
        print("Duplicate routes found.")
        route_counts = {}
        for route in routes:
            if route[0] in route_counts:
                route_counts[route[0]].append(route[1])
            else:
                route_counts[route[0]] = [route[1]]

        duplicates = [(route, endpoints) for route, endpoints in route_counts.items() if len(endpoints) > 1]
        for dup_route, endpoints in duplicates:
            print(f"The following route is duplicated: {dup_route}")
            print("It is defined in these endpoints: ", ", ".join(endpoints))

# Run the conflict check
check_route_conflicts(app)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Load a model and save it as prepared model for finetuning")
    parser.add_argument("--port", default="8080", type=str, help="server port, default 8080")
    parser.add_argument("--host", default="0.0.0.0", type=str, help="bind address, default 0.0.0.0")
    parser.add_argument("--susi_api_key", default=os.environ.get('SUSI_API_KEY', default=''), type=str, help="SUSI API key")
    parser.add_argument("--openai_api_key", default=os.environ.get('OPENAI_API_KEY', default=''), type=str, help="OpenAI API key")
    args = parser.parse_args()

    app.config['SUSI_API_KEY'] = args.susi_api_key
    app.config['OPENAI_API_KEY'] = args.openai_api_key

    serve(app, host=args.host, port=args.port, threads=8)
