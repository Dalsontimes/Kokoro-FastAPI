"""TTS service using model and voice managers."""

import os
import time
import tempfile
from typing import List, Tuple, Optional, AsyncGenerator, Union

import asyncio
import numpy as np
import torch
from loguru import logger

from ..core.config import settings
from ..inference.model_manager import get_manager as get_model_manager
from ..inference.voice_manager import get_manager as get_voice_manager
from .audio import AudioNormalizer, AudioService
from .text_processing.text_processor import process_text_chunk, smart_split
from .text_processing import tokenize
from ..inference.kokoro_v1 import KokoroV1


class TTSService:
    """Text-to-speech service."""

    # Limit concurrent chunk processing
    _chunk_semaphore = asyncio.Semaphore(4)

    def __init__(self, output_dir: str = None):
        """Initialize service."""
        self.output_dir = output_dir
        self.model_manager = None
        self._voice_manager = None

    @classmethod
    async def create(cls, output_dir: str = None) -> 'TTSService':
        """Create and initialize TTSService instance."""
        service = cls(output_dir)
        service.model_manager = await get_model_manager()
        service._voice_manager = await get_voice_manager()
        return service

    async def _process_chunk(
        self,
        chunk_text: str,
        tokens: List[int],
        voice_name: str,
        voice_path: str,
        speed: float,
        output_format: Optional[str] = None,
        is_first: bool = False,
        is_last: bool = False,
        normalizer: Optional[AudioNormalizer] = None,
    ) -> AsyncGenerator[Union[np.ndarray, bytes], None]:
        """Process tokens into audio."""
        async with self._chunk_semaphore:
            try:
                # Handle stream finalization
                if is_last:
                    # Skip format conversion for raw audio mode
                    if not output_format:
                        yield np.array([], dtype=np.float32)
                        return
                    
                    result = await AudioService.convert_audio(
                        np.array([0], dtype=np.float32),  # Dummy data for type checking
                        24000,
                        output_format,
                        is_first_chunk=False,
                        normalizer=normalizer,
                        is_last_chunk=True
                    )
                    yield result
                    return
                
                # Skip empty chunks
                if not tokens and not chunk_text:
                    return

                # Get backend
                backend = self.model_manager.get_backend()

                # Generate audio using pre-warmed model
                if isinstance(backend, KokoroV1):
                    # For Kokoro V1, pass text and voice info
                    async for chunk_audio in self.model_manager.generate(
                        chunk_text,
                        (voice_name, voice_path),
                        speed=speed
                    ):
                        # For streaming, convert to bytes
                        if output_format:
                            try:
                                converted = await AudioService.convert_audio(
                                    chunk_audio,
                                    24000,
                                    output_format,
                                    is_first_chunk=is_first,
                                    normalizer=normalizer,
                                    is_last_chunk=is_last
                                )
                                yield converted
                            except Exception as e:
                                logger.error(f"Failed to convert audio: {str(e)}")
                        else:
                            yield chunk_audio
                else:
                    # For legacy backends, load voice tensor
                    voice_tensor = await self._voice_manager.load_voice(voice_name, device=backend.device)
                    chunk_audio = await self.model_manager.generate(
                        tokens,
                        voice_tensor,
                        speed=speed
                    )
                    
                    if chunk_audio is None:
                        logger.error("Model generated None for audio chunk")
                        return
                    
                    if len(chunk_audio) == 0:
                        logger.error("Model generated empty audio chunk")
                        return
                        
                    # For streaming, convert to bytes
                    if output_format:
                        try:
                            converted = await AudioService.convert_audio(
                                chunk_audio,
                                24000,
                                output_format,
                                is_first_chunk=is_first,
                                normalizer=normalizer,
                                is_last_chunk=is_last
                            )
                            yield converted
                        except Exception as e:
                            logger.error(f"Failed to convert audio: {str(e)}")
                    else:
                        yield chunk_audio
            except Exception as e:
                logger.error(f"Failed to process tokens: {str(e)}")

    async def _get_voice_path(self, voice: str) -> Tuple[str, str]:
        """Get voice path, handling combined voices.
        
        Args:
            voice: Voice name or combined voice names (e.g., 'af_jadzia+af_jessica')
            
        Returns:
            Tuple of (voice name to use, voice path to use)
            
        Raises:
            RuntimeError: If voice not found
        """
        try:
            # Check if it's a combined voice
            if "+" in voice:
                voices = [v.strip() for v in voice.split("+") if v.strip()]
                if len(voices) < 2:
                    raise RuntimeError(f"Invalid combined voice name: {voice}")
                
                # Load and combine voices
                voice_tensors = []
                for v in voices:
                    path = await self._voice_manager.get_voice_path(v)
                    if not path:
                        raise RuntimeError(f"Voice not found: {v}")
                    logger.debug(f"Loading voice tensor from: {path}")
                    voice_tensor = torch.load(path, map_location="cpu")
                    voice_tensors.append(voice_tensor)
                
                # Average the voice tensors
                logger.debug(f"Combining {len(voice_tensors)} voice tensors")
                combined = torch.mean(torch.stack(voice_tensors), dim=0)
                
                # Save combined tensor
                temp_dir = tempfile.gettempdir()
                combined_path = os.path.join(temp_dir, f"{voice}.pt")
                logger.debug(f"Saving combined voice to: {combined_path}")
                torch.save(combined, combined_path)
                
                return voice, combined_path
            else:
                # Single voice
                path = await self._voice_manager.get_voice_path(voice)
                if not path:
                    raise RuntimeError(f"Voice not found: {voice}")
                logger.debug(f"Using single voice path: {path}")
                return voice, path
        except Exception as e:
            logger.error(f"Failed to get voice path: {e}")
            raise

    async def generate_audio_stream(
        self,
        text: str,
        voice: str,
        speed: float = 1.0,
        output_format: str = "wav",
    ) -> AsyncGenerator[bytes, None]:
        """Generate and stream audio chunks."""
        stream_normalizer = AudioNormalizer()
        chunk_index = 0
        
        try:
            # Get backend
            backend = self.model_manager.get_backend()

            # Get voice path, handling combined voices
            voice_name, voice_path = await self._get_voice_path(voice)
            logger.debug(f"Using voice path: {voice_path}")

            # Process text in chunks with smart splitting
            async for chunk_text, tokens in smart_split(text):
                try:
                    # Process audio for chunk
                    async for result in self._process_chunk(
                        chunk_text,  # Pass text for Kokoro V1
                        tokens,      # Pass tokens for legacy backends
                        voice_name,  # Pass voice name
                        voice_path,  # Pass voice path
                        speed,
                        output_format,
                        is_first=(chunk_index == 0),
                        is_last=False,  # We'll update the last chunk later
                        normalizer=stream_normalizer
                    ):
                        if result is not None:
                            yield result
                            chunk_index += 1
                        else:
                            logger.warning(f"No audio generated for chunk: '{chunk_text[:100]}...'")
                        
                except Exception as e:
                    logger.error(f"Failed to process audio for chunk: '{chunk_text[:100]}...'. Error: {str(e)}")
                    continue

            # Only finalize if we successfully processed at least one chunk
            if chunk_index > 0:
                try:
                    # Empty tokens list to finalize audio
                    async for result in self._process_chunk(
                        "",  # Empty text
                        [],  # Empty tokens
                        voice_name,
                        voice_path,
                        speed,
                        output_format,
                        is_first=False,
                        is_last=True,  # Signal this is the last chunk
                        normalizer=stream_normalizer
                    ):
                        if result is not None:
                            yield result
                except Exception as e:
                    logger.error(f"Failed to finalize audio stream: {str(e)}")

        except Exception as e:
            logger.error(f"Error in phoneme audio generation: {str(e)}")
            raise

    async def generate_audio(
        self, text: str, voice: str, speed: float = 1.0
    ) -> Tuple[np.ndarray, float]:
        """Generate complete audio for text using streaming internally."""
        start_time = time.time()
        chunks = []
        
        try:
            # Use streaming generator but collect all valid chunks
            async for chunk in self.generate_audio_stream(
                text, voice, speed,  # Default to WAV for raw audio
            ):
                if chunk is not None:
                    chunks.append(chunk)

            if not chunks:
                raise ValueError("No audio chunks were generated successfully")

            # Combine chunks, ensuring we have valid arrays
            if len(chunks) == 1:
                audio = chunks[0]
            else:
                # Filter out any zero-dimensional arrays
                valid_chunks = [c for c in chunks if c.ndim > 0]
                if not valid_chunks:
                    raise ValueError("No valid audio chunks to concatenate")
                audio = np.concatenate(valid_chunks)
            processing_time = time.time() - start_time
            return audio, processing_time

        except Exception as e:
            logger.error(f"Error in audio generation: {str(e)}")
            raise

    async def combine_voices(self, voices: List[str]) -> str:
        """Combine multiple voices."""
        return await self._voice_manager.combine_voices(voices)

    async def list_voices(self) -> List[str]:
        """List available voices."""
        return await self._voice_manager.list_voices()
