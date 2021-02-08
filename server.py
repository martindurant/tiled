from ast import literal_eval
import os
from typing import Optional
from fastapi import FastAPI, HTTPException, Query, Request, Response
from msgpack_asgi import MessagePackMiddleware

from server_utils import (
    construct_catalogs_entries_response,
    construct_datasource_response,
    DuckCatalog,
    get_dask_client,
    get_entry,
    get_chunk,
    serialize_array,
)
from queries import queries_by_name


app = FastAPI()


def add_search_routes(app=app):
    """
    Routes for search are defined at the last moment, just before startup, so
    that custom query types may be registered first.
    """
    # We bind app in a parameter above so that we have a reference to the
    # FastAPI instance itself, not the middleware which shadows it below.
    for name, query_class in queries_by_name.items():

        @app.post(f"/catalogs/search/{name}/keys/{{path:path}}")
        @app.post(f"/catalogs/search/{name}/keys", include_in_schema=False)
        async def keys_search_text(
            query: query_class,
            path: Optional[str] = "",
            offset: Optional[int] = Query(0, alias="page[offset]"),
            limit: Optional[int] = Query(10, alias="page[limit]"),
        ):
            return construct_catalogs_entries_response(
                path,
                offset,
                limit,
                query=query,
                include_metadata=False,
                include_description=False,
            )


@app.on_event("startup")
async def startup_event():
    add_search_routes()
    # Warm up the dask.distributed Cluster.
    get_dask_client()


@app.on_event("shutdown")
async def shutdown_event():
    "Gracefully shutdown the dask.distributed Client."
    client = get_dask_client()
    await client.close()


@app.get("/entry/metadata/{path:path}")
@app.get("/entry/metadata", include_in_schema=False)
async def metadata(
    path: Optional[str] = "",
):
    "Fetch the metadata for one Catalog or Data Source."

    path = path.rstrip("/")
    try:
        entry = get_entry(path)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such entry.")
    return {
        "data": {
            "id": path,
            "attributes": {"metadata": entry.metadata},
            "meta": {
                "__module__": getattr(type(entry), "__module__"),
                "__qualname__": getattr(type(entry), "__qualname__"),
            },
        }
    }


@app.get("/catalogs/entries/count/{path:path}")
@app.get("/catalogs/entries/count", include_in_schema=False)
async def entries_count(
    path: Optional[str] = "",
):
    "Fetch the number of entries in a Catalog."

    path = path.rstrip("/")
    try:
        catalog = get_entry(path)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such entry.")
    if not isinstance(catalog, DuckCatalog):
        raise HTTPException(
            status_code=404, detail="This is a Data Source, not a Catalog."
        )
    return {"data": {"id": path, "attributes": {"count": len(catalog)}}}


@app.get("/catalogs/entries/keys/{path:path}")
@app.get("/catalogs/entries/keys", include_in_schema=False)
async def keys(
    path: Optional[str] = "",
    offset: Optional[int] = Query(0, alias="page[offset]"),
    limit: Optional[int] = Query(10, alias="page[limit]"),
):
    "List only the keys of the items in a Catalog."

    return construct_catalogs_entries_response(
        path,
        offset,
        limit,
        query=None,
        include_metadata=False,
        include_description=False,
    )


@app.get("/catalogs/entries/metadata/{path:path}")
@app.get("/catalogs/entries/metadata", include_in_schema=False)
async def entries(
    path: Optional[str] = "",
    offset: Optional[int] = Query(0, alias="page[offset]"),
    limit: Optional[int] = Query(10, alias="page[limit]"),
):
    "List the keys and metadata of the items in a Catalog."

    return construct_catalogs_entries_response(
        path,
        offset,
        limit,
        query=None,
        include_metadata=True,
        include_description=False,
    )


@app.get("/catalogs/entries/description/{path:path}")
@app.get("/catalogs/entries/description", include_in_schema=False)
async def list_description(
    path: Optional[str] = "",
    offset: Optional[int] = Query(0, alias="page[offset]"),
    limit: Optional[int] = Query(10, alias="page[limit]"),
):
    "List the keys, metadata, and data structure of the items in a Catalog."

    return construct_catalogs_entries_response(
        path,
        offset,
        limit,
        query=None,
        include_metadata=True,
        include_description=False,
    )


@app.get("/datasource/description/{path:path}")
async def one_description(
    path: Optional[str],
    offset: Optional[int] = Query(0, alias="page[offset]"),
    limit: Optional[int] = Query(10, alias="page[limit]"),
):
    "Give the keys, metadata, and data structure of one Data Source."
    datasource = get_entry(path)
    # Take the response we build for /entries and augment it.
    *_, key = path.rsplit("/", 1)
    response = construct_datasource_response(
        path, key, datasource, include_metadata=True, include_description=True
    )
    return response


@app.get("/datasource/blob/array/{path:path}")
async def blob(
    request: Request,
    path: str,
    blocks: str,  # This is expected to be a list, like "[0,0]".
):
    "Provide one block (chunk) or an array."
    # Validate request syntax.
    try:
        parsed_blocks = literal_eval(blocks)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Could not parse {blocks}")
    else:
        if not isinstance(parsed_blocks, (tuple, list)) or not all(
            map(lambda x: isinstance(x, int), parsed_blocks)
        ):
            raise HTTPException(
                status_code=400, detail=f"Could not parse {blocks} as an index"
            )

    try:
        datasource = get_entry(path)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such entry.")
    try:
        chunk = datasource.read().blocks[parsed_blocks]
    except IndexError:
        raise HTTPException(status_code=422, detail="Block index out of range")
    array = await get_chunk(chunk)
    media_type = request.headers.get("Accept", "application/octet-stream")
    if media_type == "*/*":
        media_type = "application/octet-stream"
    content = await serialize_array(media_type, array)
    return Response(content=content, media_type=media_type)


# After defining all routes, wrap app with middleware.

# Add support for msgpack-encoded requests/responses as alternative to JSON.
# https://fastapi.tiangolo.com/advanced/middleware/
# https://github.com/florimondmanca/msgpack-asgi
if not os.getenv("DISABLE_MSGPACK_MIDDLEWARE"):
    app = MessagePackMiddleware(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
