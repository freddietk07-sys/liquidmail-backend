import os
import stripe
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

# Load Stripe secret key
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]


class CheckoutRequest(BaseModel):
    price_id: str
    user_email: str | None = None  # optional, Stripe allows customer creation


@router.post("/stripe/create-checkout-session")
async def create_checkout_session(data: CheckoutRequest):
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer_email=data.user_email,
            line_items=[{"price": data.price_id, "quantity": 1}],
            success_url=f"{os.environ['FRONTEND_URL']}/dashboard?success=true",
            cancel_url=f"{os.environ['FRONTEND_URL']}/dashboard?canceled=true",
        )

        # Return the URL the frontend uses to redirect
        return {"checkout_url": session.url}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    webhook_secret = os.environ["STRIPE_WEBHOOK_SECRET"]

    # Verify the webhook signature
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Subscription created
    if event["type"] == "customer.subscription.created":
        sub = event["data"]["object"]
        print("Subscription created:", sub["id"])

    # Subscription updated (e.g., plan change)
    if event["type"] == "customer.subscription.updated":
        sub = event["data"]["object"]
        print("Subscription updated:", sub["id"])

    # Subscription canceled
    if event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        print("Subscription cancelled:", sub["id"])

    return {"status": "success"}
