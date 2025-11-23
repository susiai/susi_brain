import shutil, os, time, logging, random, json
from pathlib import Path, PurePosixPath
from fastapi.security import APIKeyHeader
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi import Body, Depends, File, HTTPException, Request, UploadFile
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from werkzeug.utils import secure_filename

# set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# helper functions
def secure_path(path):
    parts = path.split('/')
    return '/'.join(secure_filename(part) for part in parts)

def confine_document_paths(path, data_path, *, searching=False):
    """Keep document requests within the configured root without following symlinks."""
    if PurePosixPath(path).is_absolute() or {"..", ".susi-history"} & set(path.split("/")) or "\\" in path:
        raise HTTPException(403)
    current = Path(data_path)
    relative = path if searching else secure_path(path)
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise HTTPException(403)


def verify_token(request: Request,
                 authorization: str | None = Depends(APIKeyHeader(name="Authorization", scheme_name="BearerToken",
                     description="Enter Bearer followed by the API key.", auto_error=False))):
    data_path = str(request.app.state.settings.data_path.resolve())
    confine_document_paths(request.path_params['req_path'], data_path)
    scheme, _, token = (authorization or '').partition(' ')
    susi_api_key = request.app.state.settings.susi_api_key.get_secret_value()
    if not susi_api_key or scheme.lower() != 'bearer' or token != susi_api_key:
        raise HTTPException(401, headers={'WWW-Authenticate': 'Bearer realm="Authentication Required"'})

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
    response = JSONResponse({'error': message})
    response.status_code = status_code
    return response

"""
FileResource Endpoints:

The FileResource class handles file storage and retrieval. It provides endpoints to upload, download, and delete files.

1. Upload:
   To upload a file, send a POST request to '/<path>' with the file data included in the request body. The '<path>' 
   is the desired path where the file should be stored. Ensure the path does not traverse outside the allowed directory.
   
   Example using curl:
   curl -X POST -H "Authorization: Bearer YOUR_ACCESS_TOKEN" -H "Content-Type: multipart/form-data" -F "file=@path/to/your/file.txt" http://<server_address>/api/v1/compute/data/path/to/store/file.txt

2. Download:
   To download a file, send a GET request to '/<path>' where '<path>' is the path to the file you wish to retrieve.
   The server will respond with the file content if the file exists and the request is authorized.

   Example using curl:
   curl -X GET -H "Authorization: Bearer YOUR_ACCESS_TOKEN" http://<server_address>/api/v1/compute/data/path/to/store/file.txt -o file.txt

3. Delete:
   To delete a file, send a DELETE request to '/<path>' where '<path>' is the path to the file you want to remove.
   The server will delete the file if it exists, the request is authorized, and the file is not in use.

   Example using curl:
   curl -X DELETE -H "Authorization: Bearer YOUR_ACCESS_TOKEN" http://<server_address>/api/v1/compute/data/path/to/store/file.txt

Please replace 'YOUR_ACCESS_TOKEN' with your actual access token and '<server_address>' with the actual address of the server.
"""
class FileResource:

    def get(self, request: Request, req_path: str):
        data_path = str(request.app.state.settings.data_path.resolve())
        confine_document_paths(req_path, data_path)
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
            response = FileResponse(abs_path, stat_result=os.stat(abs_path),
                                    filename=os.path.basename(abs_path), content_disposition_type='inline')
            if StaticFiles().is_not_modified(response.headers, request.headers):
                return NotModifiedResponse(response.headers)
            return response

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
            return {'path': req_path, 'dir': file_info}

        # Check if path is a directory
        if not os.path.exists(abs_path) or not os.path.isdir(abs_path):
            return JSONResponse(f"Directory {abs_path} does not exist.", status_code=404)
    
        # Show directory contents
        files = os.listdir(os.path.dirname(abs_path)) if req_path.endswith("index.html") else os.listdir(abs_path)
        file_links = [f'<li><a href="{os.path.join(req_path, file)}">{file}</a></li>' for file in files]
        return HTMLResponse(f'<ul>{"".join(file_links)}</ul>')

    # authorized access to upload a file to the data path
    # this can be called i.e. with:
    # curl -X POST -H 'Authorization: Bearer your-application-key' -F 'file=@/path/to/myfile.txt' http://localhost:8042/api/v1/compute/data/subdir/myfile.txt
    def post(self, request: Request, req_path: str, file: UploadFile | None = File(default=None),
             authorized=Depends(verify_token)):
        if file is None:
            return JSONResponse('No file part in the request', status_code=400)

        if file.filename == '':
            return JSONResponse('No file selected for uploading', status_code=400)

        data_path = str(request.app.state.settings.data_path.resolve())
        req_path = secure_path(req_path)
        full_path = os.path.join(data_path, req_path)

        # Prevent directory traversal
        if not is_safe_path(data_path, full_path):
            return error_response("Invalid path request", 403)

        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'wb') as destination:
                shutil.copyfileobj(file.file, destination)
            logger.debug("Stored file to: %s", req_path)
            return dict(message=f'File {os.path.basename(full_path)} uploaded successfully', status=201)
        except Exception as e:
            logger.error(f"Failed to save file {req_path}: {e}")
            return error_response("Failed to save file due to server error", 500)

    # authorized access to delete a file from the data path
    # this can be called i.e. with:
    # curl -X DELETE -H 'Authorization: Bearer your-application-key' http://localhost:8042/api/v1/compute/data/subdir/myfile.txt
    def delete(self, request: Request, req_path: str, authorized=Depends(verify_token)):
        data_path = str(request.app.state.settings.data_path.resolve())
        req_path = secure_path(req_path)
        abs_path = os.path.join(data_path, req_path)
        
        # Prevent directory traversal
        if not is_safe_path(data_path, full_path):
            return error_response("Invalid path request", 403)
        
        # Check if path is a file
        if not os.path.isfile(abs_path):
            return JSONResponse(f'File {req_path} does not exist.', status_code=404)
        
        try:
            os.remove(abs_path)
            logger.debug("deleted file from :  %s", req_path)
        
            # Remove any empty directories
            directory = os.path.dirname(abs_path)
            while directory != data_path:
                if not os.listdir(directory):  # if directory is empty
                    os.rmdir(directory)
                directory = os.path.dirname(directory)
        
            return dict(message=f'File {os.path.basename(req_path)} deleted successfully', status=200)
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

class SearchResource:
    def post(self, request: Request, search_data: dict | None = Body(default=None)):
        # Extract the search parameters from the request
        if not search_data:
            return error_response("Invalid JSON input", 400)

        query = search_data['query']
        file = search_data['file']
        max_rerank = search_data.get('max_rerank')  # Get max_rerank from input, if provided

        # Load the JSON lines file
        data_path = str(request.app.state.settings.data_path.resolve())
        confine_document_paths(file, data_path, searching=True)
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
        return matched_documents


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

