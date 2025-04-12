from flask import request, send_from_directory, Response, jsonify, current_app, make_response
from flask_restx import Namespace, Resource, fields
from flask_httpauth import HTTPTokenAuth
from werkzeug.utils import secure_filename

import os, time, logging, random, json

# set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Flask-restx namespace and authentication setup
api = Namespace('api/data', description='File hosting and search operations')
auth = HTTPTokenAuth(scheme='Bearer')

# helper functions
def secure_path(path):
    parts = path.split('/')
    return '/'.join(secure_filename(part) for part in parts)

@auth.verify_token
def verify_token(token):
    susi_api_key = current_app.config.get('SUSI_API_KEY')
    return token == susi_api_key

# check if path is safe
def is_safe_path(basedir, path, follow_symlinks=True):
    try:
        # resolves symbolic links
        if follow_symlinks:
            return os.path.realpath(path).startswith(basedir)
        else:
            return os.path.abspath(path).startswith(basedir)
    except ValueError:
        return False  # or handle the exception as needed

def error_response(message, status_code):
    response = jsonify({'error': message})
    response.status_code = status_code
    return response

"""
FileResource Endpoints:

The FileResource class handles file storage and retrieval. It provides endpoints to upload, download, and delete files.

1. Upload:
   To upload a file, send a POST request to '/<path>' with the file data included in the request body. The '<path>' 
   is the desired path where the file should be stored. Ensure the path does not traverse outside the allowed directory.
   
   Example using curl:
   curl -X POST -H "Authorization: Bearer YOUR_ACCESS_TOKEN" -H "Content-Type: multipart/form-data" -F "file=@path/to/your/file.txt" http://<server_address>/api/data/path/to/store/file.txt

2. Download:
   To download a file, send a GET request to '/<path>' where '<path>' is the path to the file you wish to retrieve.
   The server will respond with the file content if the file exists and the request is authorized.

   Example using curl:
   curl -X GET -H "Authorization: Bearer YOUR_ACCESS_TOKEN" http://<server_address>/api/data/path/to/store/file.txt -o file.txt

3. Delete:
   To delete a file, send a DELETE request to '/<path>' where '<path>' is the path to the file you want to remove.
   The server will delete the file if it exists, the request is authorized, and the file is not in use.

   Example using curl:
   curl -X DELETE -H "Authorization: Bearer YOUR_ACCESS_TOKEN" http://<server_address>/api/data/path/to/store/file.txt

Please replace 'YOUR_ACCESS_TOKEN' with your actual access token and '<server_address>' with the actual address of the server.
"""
@api.route('/<path:req_path>', methods=['GET', 'POST', 'DELETE'])
class FileResource(Resource):

    @api.doc('get_file')
    def get(self, req_path):
        data_path = current_app.config['DATA_PATH']
        req_path = secure_path(req_path)
        abs_path = os.path.join(data_path, req_path)
        logger.debug("data_path:    %s", data_path)
        logger.debug("req_path:     %s", req_path)
        logger.debug("abs_path:     %s", abs_path)

        # Check for directory traversal attempts
        if not is_safe_path(data_path, abs_path):
            return error_response("Invalid path request", 403)

        # Check if path is a file and serve
        if os.path.isfile(abs_path):
            return send_from_directory(data_path, req_path)

        # Check if an index.json file is requested
        if req_path.endswith("index.json"):
            parent_path = os.path.dirname(abs_path.rstrip("index.json"))
            files = [] if not os.path.exists(parent_path) else os.listdir(parent_path)
            file_info = []
            for file in files:
                file_abs_path = os.path.join(abs_path.rstrip("index.json"), file)
                stats = os.stat(file_abs_path)
                file_info.append({
                    "name": file, # file name
                    "size": stats.st_size, # file size in bytes
                    "atime": int(stats.st_atime * 1000), # access time in millisonde since epoch
                    "mtime": int(stats.st_mtime * 1000), # modified time in millisonde since epoch
                    "last_accessed": time.ctime(stats.st_atime),
                    "last_modified": time.ctime(stats.st_mtime)
                })
            return jsonify({'path': req_path, 'dir': file_info})

        # Check if path is a directory
        if not os.path.exists(abs_path) or not os.path.isdir(abs_path):
            return f"Directory {abs_path} does not exist.", 404
    
        # Show directory contents
        files = os.listdir(os.path.dirname(abs_path)) if req_path.endswith("index.html") else os.listdir(abs_path)
        file_links = [f'<li><a href="{os.path.join(req_path, file)}">{file}</a></li>' for file in files]
        return Response(f'<ul>{"".join(file_links)}</ul>', mimetype='text/html')

    # authorized access to upload a file to the data path
    # this can be called i.e. with:
    # curl -X POST -H 'Authorization: Bearer your-application-key' -F 'file=@/path/to/myfile.txt' http://localhost:8080/subdir/myfile.txt
    @api.doc('upload_file')
    @auth.login_required
    def post(self, req_path):
        if 'file' not in request.files:
            return 'No file part in the request', 400
        file = request.files['file']

        if file.filename == '':
            return 'No file selected for uploading', 400

        data_path = current_app.config['DATA_PATH']
        req_path = secure_path(req_path)
        full_path = os.path.join(data_path, req_path)

        # Prevent directory traversal
        if not is_safe_path(data_path, full_path):
            return error_response("Invalid path request", 403)

        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            file.save(full_path)
            logger.debug("Stored file to: %s", req_path)
            return jsonify(message=f'File {os.path.basename(full_path)} uploaded successfully', status=201)
        except Exception as e:
            logger.error(f"Failed to save file {req_path}: {e}")
            return error_response("Failed to save file due to server error", 500)

    # authorized access to delete a file from the data path
    # this can be called i.e. with:
    # curl -X DELETE -H 'Authorization: Bearer your-application-key' http://localhost:8080/subdir/myfile.txt
    @api.doc('delete_file')
    @auth.login_required
    def delete(self, req_path):
        data_path = current_app.config['DATA_PATH']
        req_path = secure_path(req_path)
        abs_path = os.path.join(data_path, req_path)
        
        # Prevent directory traversal
        if not is_safe_path(data_path, full_path):
            return error_response("Invalid path request", 403)
        
        # Check if path is a file
        if not os.path.isfile(abs_path):
            return f'File {req_path} does not exist.', 404
        
        try:
            os.remove(abs_path)
            logger.debug("deleted file from :  %s", req_path)
        
            # Remove any empty directories
            directory = os.path.dirname(abs_path)
            while directory != data_path:
                if not os.listdir(directory):  # if directory is empty
                    os.rmdir(directory)
                directory = os.path.dirname(directory)
        
            return jsonify(message=f'File {os.path.basename(req_path)} deleted successfully', status=200)
        except FileNotFoundError:
            return error_response(f'File {req_path} does not exist.', 404)
        except Exception as e:
            logger.error(f"Failed to delete file {req_path}: {e}")
            return error_response("Failed to delete file due to server error", 500)

"""
The Search endpoint (/search) makes a search over a set of documents.
This requires that there is already a set of documents was stored in a jsonl file before.
Consider that the sharing endpoint has stored a file "vogon.jsonl" with the following contents:
```
{"sku": "1", "text_t": "Oh freddled gruntbuggly, thy micturations are to me As plurdled gabbleblotchits on a lurgid bee. Groop, I implore thee, my foonting turlingdromes."}
{"sku": "2", "text_t": "And hooptiously drangle me with crinkly bindlewurdles, Or I will rend thee in the gobberwarts with my blurglecruncheon, see if I don't!"}
```

now we can search the file with the following command:
```
curl  http://localhost:8080/search \
  -X POST \
  -H 'Content-Type: application/json' \
  -d '{
    "file": "vogon.jsonl",
    "query": "oh bee",
    "max_rerank": 5
  }'
```

The above command would give the following output:
```
[
    {
        "sku": "1",
        "text_t": "Oh freddled <and so on, the whole thing>"",
        "file_id": "vogon.jsonl",
        "score": 42.69
    }
]
```

The response is computed in the following way:
- the query is used to make a search over the documents in the file where all words must match
- if the search results has less than `max_rerank` documents, then the search is repeated with a fuzzy search
- the `max_rerank` documents are ranked for similarity with the query and ordered by their score
- the top `max_rerank` documents are returned
 
The score has a range from 0 to 300 where 200 and more means that the document is semantically
similar to the query.
"""

# Define the model for the search input
search_input = api.model('SearchInput', {
    'file': fields.String(required=True, description='The filename to search in'),
    'query': fields.String(required=True, description='The query to search for'),
    'max_rerank': fields.Integer(required=False, description='Maximum number of documents to rerank')
})

@api.route('/search', methods=['POST'])
class SearchResource(Resource):
    @api.expect(search_input)
    def post(self):
        # Extract the search parameters from the request
        search_data = request.get_json()
        if not search_data:
            return error_response("Invalid JSON input", 400)

        query = search_data['query']
        file = search_data['file']
        max_rerank = search_data.get('max_rerank')  # Get max_rerank from input, if provided

        # Load the JSON lines file
        data_path = current_app.config['DATA_PATH']
        file_path = os.path.join(data_path, file)
        if not os.path.isfile(file_path):
            return error_response("File not found", 404)

        # Read the file
        documents = []
        with open(file_path, 'r') as f:
            documents = [json.loads(line) for line in f]

        # Tokenize the search query
        search_tokens = set(query.lower().split())

        # Use the simple_search function with max_rerank
        matched_documents = simple_search(search_tokens, documents, max_rerank)

        # Return the matched documents
        return jsonify(matched_documents)


def simple_search(search_tokens, documents, max_rerank=None):
    """
    Perform a simple search for documents containing all the search tokens,
    returning up to max_rerank documents.
    
    :param search_tokens: A set of tokens to search for
    :param documents: A list of documents to search through
    :param max_rerank: The maximum number of documents to return
    :return: A list of documents that match the search tokens
    """
    matched_documents = []
    for doc in documents:
        # Stop if we have reached the max_rerank number of documents
        if max_rerank is not None and len(matched_documents) >= max_rerank:
            break
        
        doc_tokens = set(doc['text_t'].lower().split())
        if search_tokens.issubset(doc_tokens):
            matched_documents.append(doc)
    
    return matched_documents

