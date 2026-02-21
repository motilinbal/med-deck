"""
Notification Hub for MedDeck Server.

This module provides a centralized WebSocket notification manager that routes
messages from backend services to frontend clients. It acts as a switchboard
for real-time updates.

Usage:
    from app.services.notification_hub import NotificationHub
    
    hub = NotificationHub()
    
    # In WebSocket endpoint:
    await hub.connect(websocket, card_id)
    
    # From anywhere in the app:
    await hub.notify_new_mail(card_id, {"has_pdf": True, "chunks": 3})
    await hub.trigger_sync()  # Broadcast to all clients
"""

import logging
from typing import Dict, List, Any, Optional
from fastapi import WebSocket
from bson.objectid import ObjectId

import database

logger = logging.getLogger(__name__)


class NotificationHub:
    """
    Centralized WebSocket notification manager.
    
    Manages active WebSocket connections mapped to card_ids and provides
    methods for sending targeted or broadcast messages.
    """
    
    def __init__(self):
        """Initialize the notification hub with empty connection tracking."""
        # Map card_id -> List of active WebSocket connections
        self.connections: Dict[str, List[WebSocket]] = {}
    
    async def connect(self, websocket: WebSocket, card_id: str):
        """
        Accept a new WebSocket connection and register it.

        Also sends any stored undelivered events for this card.

        Args:
            websocket: The WebSocket connection to accept
            card_id: The card ID this connection is associated with
        """
        await websocket.accept()

        if card_id not in self.connections:
            self.connections[card_id] = []

        self.connections[card_id].append(websocket)
        logger.info(f"WebSocket connected for card {card_id}. Total connections: {len(self.connections[card_id])}")

        # Send any stored undelivered events for this card
        await self._send_stored_events(websocket, card_id)
    
    def disconnect(self, websocket: WebSocket, card_id: str):
        """
        Remove a WebSocket connection from tracking.
        
        Args:
            websocket: The WebSocket connection to remove
            card_id: The card ID this connection was associated with
        """
        if card_id in self.connections:
            try:
                self.connections[card_id].remove(websocket)
                logger.info(f"WebSocket disconnected for card {card_id}. Remaining: {len(self.connections[card_id])}")
                
                # Clean up empty lists to prevent memory bloat
                if not self.connections[card_id]:
                    del self.connections[card_id]
            except ValueError:
                # Socket wasn't in the list (already removed or never added)
                pass

    async def _send_stored_events(self, websocket: WebSocket, card_id: str):
        """
        Fetch and send any stored undelivered events for a card.

        Args:
            websocket: The WebSocket to send events to
            card_id: The card ID to get events for
        """
        try:
            events = await database.get_undelivered_events(card_id)
            if events:
                logger.info(f"Sending {len(events)} stored events to card {card_id}")
                for event in events:
                    message = {
                        "type": "system_event",
                        "event_category": event["event_category"],
                        "card_id": event["card_id"],
                        "payload": event["payload"]
                    }
                    try:
                        await websocket.send_json(message)
                    except Exception as e:
                        logger.warning(f"Failed to send stored event to card {card_id}: {e}")
        except Exception as e:
            logger.error(f"Error sending stored events for card {card_id}: {e}")

    async def emit_system_event(self, card_id: str, category: str, payload: Dict[str, Any]):
        """
        Send a system event message to all connections for a specific card.

        Constructs a standardized message envelope and sends it to all
        WebSockets associated with the given card_id.

        If no connections exist, stores the event for later delivery when client reconnects.

        Args:
            card_id: The target card ID
            category: Event category (e.g., "new_arrival", "process_status")
            payload: Event-specific data payload
        """
        if card_id not in self.connections:
            # Store event for later delivery when client reconnects
            await database.store_undelivered_event(card_id, category, payload)
            logger.debug(f"No connection for card {card_id}, stored event for later: {category}")
            return
        
        message = {
            "type": "system_event",
            "event_category": category,
            "card_id": card_id,
            "payload": payload
        }
        
        # Track dead sockets for cleanup
        dead_sockets = []
        
        for socket in self.connections[card_id]:
            try:
                await socket.send_json(message)
            except Exception as e:
                # Socket is likely closed or broken
                logger.warning(f"Failed to send message to socket for card {card_id}: {e}")
                dead_sockets.append(socket)
        
        # Clean up dead sockets
        for dead_socket in dead_sockets:
            try:
                self.connections[card_id].remove(dead_socket)
            except ValueError:
                pass
        
        # Remove empty card entry if all sockets are dead
        if not self.connections[card_id]:
            del self.connections[card_id]
    
    async def broadcast_event(self, event_category: str, payload: Dict[str, Any], card_id: str = None):
        """
        Broadcast an event to ALL connected clients across all cards.
        
        Used for global events like sync_cards that affect all users.
        
        Args:
            event_category: Event category (e.g., "sync_cards")
            payload: Event-specific data payload
            card_id: Optional card ID to include in the event (for targeted notifications)
        """
        message = {
            "type": "system_event",
            "event_category": event_category,
            "card_id": card_id,  # Include card_id if provided
            "payload": payload
        }
        
        # Track dead sockets and their card_ids for cleanup
        dead_sockets: List[tuple] = []  # (card_id, socket)
        
        for card_id, sockets in self.connections.items():
            for socket in sockets:
                try:
                    await socket.send_json(message)
                except Exception as e:
                    logger.warning(f"Failed to broadcast to socket for card {card_id}: {e}")
                    dead_sockets.append((card_id, socket))
        
        # Clean up dead sockets
        for card_id, dead_socket in dead_sockets:
            try:
                self.connections[card_id].remove(dead_socket)
            except ValueError:
                pass
            
            # Remove empty card entries
            if card_id in self.connections and not self.connections[card_id]:
                del self.connections[card_id]
    
    # =========================================================================
    # Convenience Helper Methods
    # =========================================================================
    
    # async def notify_new_mail(self, card_id: str, summary_data: Dict[str, Any]):
    #     """
    #     Notify that new mail has arrived for a specific card.
        
    #     Args:
    #         card_id: The target card ID
    #         summary_data: Dict containing meta info like:
    #             - pending_id: str
    #             - has_pdf: bool
    #             - text_chunks: int
    #             - sender: str
    #             - timestamp: str (ISO format)
    #     """
    #     await self.emit_system_event(
    #         card_id=card_id,
    #         category="new_arrival",
    #         payload=summary_data
    #     )
    #     logger.info(f"Notified new mail for card {card_id}")

    async def notify_new_mail(self, card_id: str, summary_data: Dict[str, Any]):
        """
        Notify ALL connected clients that new mail has arrived.
        """
        # We broadcast this because we want Global Toasts to appear
        # regardless of which card the user is currently looking at.
        
        # Look up the card to get its serial number
        from database import cards_collection
        card = await cards_collection.find_one({"_id": ObjectId(card_id)})
        card_serial = card.get("serial") if card else None
        
        await self.broadcast_event(
            event_category="new_arrival",
            payload={
                **summary_data,
                "card_id": card_id,
                "card_serial": card_serial  # Include card serial for frontend display
            },
            card_id=card_id  # Pass card_id to the broadcast_event so it's in the root of the message
        )
        logger.info(f"Broadcasted new mail notification for card {card_id}")
    
    async def notify_progress(self, card_id: str, message: str, state: str = "processing"):
        """
        Send a progress update during ingestion processing.
        
        Args:
            card_id: The target card ID
            message: Human-readable progress message (e.g., "Scanning PDF tables...")
            state: One of "processing", "success", "error"
        """
        await self.emit_system_event(
            card_id=card_id,
            category="process_status",
            payload={
                "message": message,
                "state": state
            }
        )
    
    async def trigger_sync(self):
        """
        Trigger a card list sync for all connected clients.
        
        Used when a new card is created (Patient X) or when a provisional
        card is deleted, requiring all clients to refresh their card lists.
        """
        await self.broadcast_event(
            event_category="sync_cards",
            payload={}
        )
        logger.info("Triggered sync_cards broadcast to all clients")


# Global singleton instance
# Import this instance to use throughout the application
notification_hub = NotificationHub()
