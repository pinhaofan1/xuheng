"""Explicit isolated production API for validation; normal startup never imports it."""
import argparse
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


def register_identity(app,run_id):
    @app.get('/internal/validation-status',include_in_schema=False)
    def identity():
        return {'run_id':run_id,'pid':os.getpid(),'isolated':True}
    # Production already registers a SPA catch-all. The explicit validation
    # identity route must precede it; HTML with HTTP 200 is not API readiness.
    app.router.routes.insert(0,app.router.routes.pop())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8013)
    parser.add_argument('--run-id',required=True)
    args=parser.parse_args()
    from scripts.validation.goal_workspace import configure_environment
    configure_environment()
    from interfaces.main import app
    register_identity(app,args.run_id)
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=args.port)


if __name__=='__main__':
    main()
