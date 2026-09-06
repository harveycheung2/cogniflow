import http.server
import socketserver

PORT = 8502
TARGET = "http://localhost:8765"

class RedirectHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", f"{TARGET}{self.path}")
        self.end_headers()

    def do_POST(self):
        self.send_response(307)
        self.send_header("Location", f"{TARGET}{self.path}")
        self.end_headers()

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), RedirectHandler) as httpd:
        print(f"Redirecting from http://localhost:{PORT} to {TARGET}")
        httpd.serve_forever()
