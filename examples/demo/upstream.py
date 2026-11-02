"""Small in-memory orders API for the SpecGuard walkthrough."""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


async def create_order(request: Request) -> JSONResponse:
    state = request.app.state
    state.received_requests += 1
    data = await request.json()
    # Contract validation belongs to SpecGuard. This upstream simply stores data.
    order = {
        "id": len(state.orders) + 1,
        "name": data.get("name"),
        "quantity": data.get("quantity"),
        "status": data.get("status", "draft"),
    }
    state.orders.append(order)
    return JSONResponse(order, status_code=201, headers={"X-Demo-Upstream": "orders"})


async def list_orders(request: Request) -> JSONResponse:
    request.app.state.received_requests += 1
    limit = int(request.query_params.get("limit", "10"))
    return JSONResponse(request.app.state.orders[-limit:])


async def get_order(request: Request) -> JSONResponse:
    state = request.app.state
    state.received_requests += 1
    order_id = request.path_params["order_id"]
    if not 1 <= order_id <= len(state.orders):
        return JSONResponse({"error": "Order not found."}, status_code=404)
    return JSONResponse(state.orders[order_id - 1])


async def demo_stats(request: Request) -> JSONResponse:
    # Inspection does not count as a business request or change the demo state.
    return JSONResponse({"received_requests": request.app.state.received_requests})


def create_app() -> Starlette:
    app = Starlette(
        routes=[
            Route("/orders", create_order, methods=["POST"]),
            Route("/orders", list_orders, methods=["GET"]),
            Route("/orders/{order_id:int}", get_order, methods=["GET"]),
            Route("/_demo/stats", demo_stats, methods=["GET"]),
        ]
    )
    app.state.orders = []
    app.state.received_requests = 0
    return app


app = create_app()
