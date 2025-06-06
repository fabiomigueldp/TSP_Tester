#!/usr/bin/env python3
"""
tsp_tester.py - Ferramenta Profissional de Teste para o Trading-Signal-Processor (v2.2 Corrigida).

Esta aplicação é um utilitário completo de teste de carga e funcionalidade,
desenhado especificamente para o microserviço Trading-Signal-Processor.

Funcionalidades:
- Envio de sinais com tickers pré-definidos para teste de aprovação e rejeição.
- Padrão de envio de requisições suave e distribuído ao longo do tempo.
- Controle de proporção entre sinais que devem ser aprovados vs. rejeitados.
- Templating de payload dinâmico com Faker para dados realistas.
- Controles de RPS, período de ramp-up e duração do teste.
- Monitoramento em tempo real do estado interno da aplicação via API polling.
- Verificação de integridade comparando dados enviados vs. processados pela aplicação.
- Gráficos em tempo real de RPS, latência, tamanho da fila e contagens de sinais.
- Painel de controle para interagir com os endpoints de admin da aplicação em tempo real.
- Gerenciamento de perfis de teste (salvar/carregar).
- Exportação de resultados detalhados em CSV.
- Modo Headless/CLI para testes automatizados.
"""

import asyncio
import httpx
import json
import random
import sys
import time
import threading
import queue
import argparse
from collections import Counter, deque
from typing import List, Dict, Optional
from faker import Faker

# Tkinter and Matplotlib imports
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter.scrolledtext import ScrolledText
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib import animation
import matplotlib.animation as animation

# --- Constants and Global Faker Instance ---
APP_VERSION = "TSP Tester v2.2"
DEFAULT_PAYLOAD_TEMPLATE = """{{
    "ticker": "{ticker}",
    "side": "{faker.random_element(elements=('BUY', 'SELL'))}",
    "price": {faker.pyfloat(left_digits=3, right_digits=2, positive=True)},
    "time": "{faker.iso8601()}"
}}"""
MAX_GRAPH_POINTS = 100 # Number of data points to show on the graph
fake = Faker()

# ─────────────────── ASYNC WORKER (The Engine) ───────────────────
class AsyncWorker:
    """Encapsulates all asynchronous network logic, running in a separate thread."""

    def __init__(self, config: Dict, output_queue: queue.Queue, shutdown_event: threading.Event):
        self.config = config
        self.config_lock = threading.Lock()  # Para acesso thread-safe
        self.output_queue = output_queue
        self.shutdown_event = shutdown_event
        self.test_start_time = 0

    def update_config(self, new_config: Dict):
        """Atualiza a configuração dinamicamente durante o teste"""
        with self.config_lock:
            # Atualizar apenas campos que podem ser alterados em tempo real
            dynamic_fields = ['rps', 'approved_ratio', 'ramp_up_s']
            for field in dynamic_fields:
                if field in new_config:
                    old_value = self.config.get(field)
                    self.config[field] = new_config[field]
                    if old_value != new_config[field]:
                        self.log_to_gui(f"Config updated: {field} = {new_config[field]} (was {old_value})", "log")

    def run(self):
        """Thread entry point. Sets up and runs the asyncio event loop."""
        self.test_start_time = time.monotonic()
        try:
            asyncio.run(self.request_dispatcher())
        except Exception as e:
            self.log_to_gui(f"[FATAL ERROR] Worker thread crashed: {e}", "log")

    def log_to_gui(self, message, msg_type):
        """Puts a message onto the queue for the GUI to process."""
        self.output_queue.put({"type": msg_type, "payload": message})

    def _generate_payload(self, template: str, ticker: str) -> Dict:
        try:
            resolved_template = eval(f'f"""{template}"""', {"faker": fake, "ticker": ticker})
            return json.loads(resolved_template)
        except Exception as e:
            self.log_to_gui(f"[WARNING] Payload generation failed: {e}", "log")
            return {"error": "payload_generation_failed", "ticker": ticker}

    def _build_request_batch(self) -> List[str]:
        with self.config_lock:
            approved_items = self.config['approved_tickers']
            rejected_items = self.config['rejected_tickers']
            ramp_up_time = self.config['ramp_up_s']
            target_rps = self.config['rps']
            approved_ratio = self.config['approved_ratio']
        
        elapsed = time.monotonic() - self.test_start_time

        if ramp_up_time > 0 and elapsed < ramp_up_time:
            current_rps = int(target_rps * (elapsed / ramp_up_time))
        else:
            current_rps = target_rps
        
        if current_rps == 0: return []

        num_approved_to_send = int(current_rps * (approved_ratio / 100.0))
        num_rejected_to_send = current_rps - num_approved_to_send
        
        batch = []
        if num_approved_to_send > 0 and approved_items:
            batch.extend(random.choices(approved_items, k=num_approved_to_send))
        if num_rejected_to_send > 0 and rejected_items:
            batch.extend(random.choices(rejected_items, k=num_rejected_to_send))
            
        random.shuffle(batch)
        return batch

    async def _send_one(self, client: httpx.AsyncClient, sem: asyncio.Semaphore, ticker: str):
        with self.config_lock:
            is_approved_ticker = ticker in self.config['approved_tickers']
            payload_template = self.config['payload_template']
            url = self.config['url']
            headers = self.config['headers']
        
        payload = self._generate_payload(payload_template, ticker)
        
        start_time = time.monotonic()
        latency, status_code, error_detail = -1, -1, "Unknown"

        async with sem:
            try:
                resp = await client.post(url, headers=headers, json=payload, timeout=10.0)
                latency = time.monotonic() - start_time
                status_code = resp.status_code
                resp.raise_for_status()
                error_detail = f"HTTP {status_code}"
            except httpx.HTTPStatusError as e:
                error_detail = f"HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                error_detail = type(e).__name__
            except Exception as e:
                error_detail = f"Generic: {type(e).__name__}"

        self.output_queue.put({"type": "sent_result", "payload": {"status_code": status_code, "latency": latency, "error": error_detail, "ticker": ticker, "was_approved_ticker": is_approved_ticker}})

    async def request_dispatcher(self):
        self.log_to_gui("Worker thread started. Test running...", "log")
        semaphore = asyncio.Semaphore(self.config['max_concurrency'])
        async with httpx.AsyncClient() as client:
            while not self.shutdown_event.is_set():
                loop_start_time = time.monotonic()
                batch = self._build_request_batch()
                
                if not batch:
                    # Sleep em chunks menores para ser mais responsivo ao shutdown
                    for _ in range(10):  # 10 * 0.1s = 1s total
                        if self.shutdown_event.is_set():
                            break
                        await asyncio.sleep(0.1)
                    continue

                tasks = []
                for i, ticker in enumerate(batch):
                    if self.shutdown_event.is_set(): break
                    
                    # Spreading logic from dual_debug.py
                    desired_launch_time = loop_start_time + (i / len(batch))
                    sleep_duration = desired_launch_time - time.monotonic()
                    if sleep_duration > 0:
                        try:
                            await asyncio.sleep(sleep_duration)
                        except asyncio.CancelledError:
                            break
                    
                    if self.shutdown_event.is_set(): break
                    tasks.append(asyncio.create_task(self._send_one(client, semaphore, ticker)))

                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                
                # Sleep for the remainder of the 1-second cycle
                elapsed = time.monotonic() - loop_start_time
                remaining_sleep = 1.0 - elapsed
                if remaining_sleep > 0:
                    # Sleep em chunks menores para resposta mais rápida ao shutdown
                    sleep_chunks = max(1, int(remaining_sleep / 0.1))
                    chunk_duration = remaining_sleep / sleep_chunks
                    for _ in range(sleep_chunks):
                        if self.shutdown_event.is_set():
                            break
                        try:
                            await asyncio.sleep(chunk_duration)
                        except asyncio.CancelledError:
                            break
                
                # Verificar novamente o shutdown_event após o sleep
                if self.shutdown_event.is_set():
                    break

        self.log_to_gui("Worker thread finished.", "log")
        self.output_queue.put({"type": "test_finished", "payload": None})


# ─────────────────── APP STATUS POLLER ───────────────────
class AppStatusPoller:
    """Polls the target application's status endpoint in a separate thread."""
    def __init__(self, config: Dict, output_queue: queue.Queue, shutdown_event: threading.Event):
        self.config = config; self.output_queue = output_queue
        self.shutdown_event = shutdown_event
        self.status_url = self.config['app_status_url']
        self.client = httpx.Client(timeout=2.0)

    def run(self):
        while not self.shutdown_event.is_set():
            try:
                resp = self.client.get(self.status_url); resp.raise_for_status()
                self.output_queue.put({"type": "app_status", "payload": resp.json()})
            except (httpx.RequestError, json.JSONDecodeError, KeyError) as e:
                self.output_queue.put({"type": "app_status", "payload": {"error": f"Poll Fail: {type(e).__name__}"}})
            self.shutdown_event.wait(2.0)
        self.client.close()
        
# ─────────────────── TEST CONTROLLER (The Brain) ───────────────────
class TestController:
    """Controls test execution and bridges GUI <-> AsyncWorker."""

    def __init__(self, app: 'ProStressTesterApp'):
        self.app = app
        self.state = "IDLE"
        self.worker_thread: Optional[threading.Thread] = None
        self.poller_thread: Optional[threading.Thread] = None
        self.shutdown_event: Optional[threading.Event] = None
        self.output_queue = queue.Queue(); self.state = "IDLE" 
        self.duration_timer_id = None; self.reset_stats()

    def reset_stats(self):
        self.start_time = 0; self.total_sent = 0; self.sent_approved = 0; self.sent_rejected = 0
        self.response_success = 0; self.response_fail = 0; self.error_counter = Counter()
        self.latencies = []; self.last_app_status = {}
        if hasattr(self, 'app') and self.app:
            self.app.clear_graphs(); self.app.clear_output_log(); self.app.update_app_status_display({})

    def start_test(self):
        if self.state == "RUNNING": return
        config = self.app.get_current_config()
        if not config: return
        self.reset_stats(); self.start_time = time.monotonic()
        self.state = "RUNNING"; self.app.set_control_state(self.state)
        self.shutdown_event = threading.Event()
        
        self.worker = AsyncWorker(config, self.output_queue, self.shutdown_event)
        self.worker_thread = threading.Thread(target=self.worker.run, daemon=True); self.worker_thread.start()
        
        poller = AppStatusPoller(config, self.output_queue, self.shutdown_event)
        self.poller_thread = threading.Thread(target=poller.run, daemon=True); self.poller_thread.start()

        duration_s = config.get('duration_s')
        if duration_s and duration_s > 0:
            self.duration_timer_id = self.app.master.after(duration_s * 1000, self.auto_stop_test)
        
        self.app.master.after(100, self.process_queue)

    def auto_stop_test(self):
        self.app.log_output(f"Test duration reached. Automatically stopping...")
        self.stop_test()

    def stop_test(self):
        if self.state != "RUNNING": return
        if self.duration_timer_id:
            self.app.master.after_cancel(self.duration_timer_id); self.duration_timer_id = None
        self.state = "STOPPING"; self.app.set_control_state(self.state)
        self.app.log_output("Stopping test...")
        if self.shutdown_event: 
            self.shutdown_event.set()
            # Agendar verificação das threads em background para não travar GUI
            self.app.master.after(100, self._check_stop_completion)

    def _check_stop_completion(self):
        """Verifica se as threads terminaram sem bloquear a GUI"""
        threads_finished = True
        
        if self.worker_thread and self.worker_thread.is_alive():
            threads_finished = False
        if self.poller_thread and self.poller_thread.is_alive():
            threads_finished = False
            
        if threads_finished:
            # Todas as threads terminaram
            self.state = "FINISHED"
            self.app.set_control_state(self.state)
            self.app.log_output("--- TEST STOPPED ---")
        else:
            # Ainda há threads rodando, verificar novamente em 100ms
            self.app.master.after(100, self._check_stop_completion)

    def process_queue(self):
        try:
            while not self.output_queue.empty():
                msg = self.output_queue.get_nowait()
                msg_type, payload = msg.get("type"), msg.get("payload")

                if msg_type == "log": self.app.log_output(payload)
                elif msg_type == "sent_result":
                    self.total_sent += 1
                    if payload['was_approved_ticker']: self.sent_approved += 1
                    else: self.sent_rejected += 1
                    if 200 <= payload['status_code'] < 300:
                        self.response_success += 1
                        if payload['latency'] != -1: self.latencies.append(payload['latency'])
                    else:
                        self.response_fail += 1
                        self.error_counter[payload['error']] += 1
                elif msg_type == "app_status":
                    self.last_app_status = payload; self.app.update_app_status_display(payload)
                elif msg_type == "test_finished":
                    # Só mudar estado se não estivermos já parando manualmente
                    if self.state != "STOPPING":
                        if self.poller_thread and self.shutdown_event:
                            self.shutdown_event.set(); self.poller_thread.join(timeout=1)
                        self.state = "FINISHED"; self.app.set_control_state(self.state)
                        self.app.log_output("--- TEST FINISHED ---")
                    return
        except queue.Empty: pass
        if self.state in ["RUNNING", "STOPPING"]: self.app.master.after(100, self.process_queue)
    
    def update_stats_display(self, *args):
        if self.state != "RUNNING": return
        elapsed_time = time.monotonic() - self.start_time
        rps = self.total_sent / elapsed_time if elapsed_time > 0 else 0
        avg_lat = (sum(self.latencies) / len(self.latencies)) if self.latencies else 0
        app_metrics = self.last_app_status.get('signal_processing', {})
        app_approved = app_metrics.get('signals_approved', 0)
        app_rejected = app_metrics.get('signals_rejected', 0)
        
        self.app.update_live_stats({
            "rps": f"{rps:.2f}", "total_sent": self.total_sent, "sent_approved": self.sent_approved,
            "sent_rejected": self.sent_rejected, "resp_success": self.response_success, "resp_fail": self.response_fail,
            "avg_lat": f"{avg_lat * 1000:.2f} ms", "app_approved": app_approved, "app_rejected": app_rejected,
            "mismatch_approved": self.sent_approved - app_approved, "mismatch_rejected": self.sent_rejected - app_rejected,
        })
        queue_size = self.last_app_status.get('queue', {}).get('current_size', 0)
        self.app.add_graph_data(t=elapsed_time, rps=rps, latency_ms=avg_lat * 1000, queue_size=queue_size, sent_approved=self.sent_approved, app_approved=app_approved)
        if len(self.latencies) > 5000: self.latencies = self.latencies[-2500:]

    def update_config_realtime(self, new_config: Dict):
        """Atualiza configuração em tempo real durante teste ativo"""
        if self.state != "RUNNING":
            return
        
        if hasattr(self, 'worker') and self.worker:
            self.worker.update_config(new_config)
            self.app.log_output(f"Real-time config update: {new_config}")
        else:
            self.app.log_output("[WARNING] Cannot update config: worker not available")

    def save_config(self):
        """Salva o perfil de teste atual em arquivo JSON"""
        if not self.app:
            return
        
        config = self.app.get_current_config()
        if not config:
            return
        
        # Remover campos internos que não devem ser salvos
        save_config = config.copy()
        save_config.pop('url', None)
        save_config.pop('app_status_url', None)
        save_config.pop('max_concurrency', None)
        
        filename = filedialog.asksavefilename(
            title="Save Test Profile",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        
        if filename:
            try:
                with open(filename, 'w') as f:
                    json.dump(save_config, f, indent=4)
                self.app.log_output(f"Test profile saved to: {filename}")
                messagebox.showinfo("Success", f"Test profile saved successfully to:\n{filename}")
            except Exception as e:
                error_msg = f"Failed to save test profile: {e}"
                self.app.log_output(error_msg)
                messagebox.showerror("Error", error_msg)

    def load_config(self):
        """Carrega um perfil de teste de arquivo JSON"""
        if not self.app:
            return
        
        filename = filedialog.askopenfilename(
            title="Load Test Profile",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        
        if filename:
            try:
                with open(filename, 'r') as f:
                    config = json.load(f)
                
                self.app.set_current_config(config)
                self.app.log_output(f"Test profile loaded from: {filename}")
                messagebox.showinfo("Success", f"Test profile loaded successfully from:\n{filename}")
            except Exception as e:
                error_msg = f"Failed to load test profile: {e}"
                self.app.log_output(error_msg)
                messagebox.showerror("Error", error_msg)

    def export_results(self):
        """Exporta os resultados do teste para arquivo CSV"""
        if self.state != "FINISHED":
            messagebox.showwarning("Warning", "No test results to export. Please run a test first.")
            return
        
        filename = filedialog.asksavefilename(
            title="Export Test Results",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        
        if filename:
            try:
                import csv
                elapsed_time = time.monotonic() - self.start_time if self.start_time > 0 else 0
                avg_latency = (sum(self.latencies) / len(self.latencies)) if self.latencies else 0
                
                app_metrics = self.last_app_status.get('signal_processing', {})
                
                # Dados do resultado
                results = [
                    ["Metric", "Value"],
                    ["Test Duration (s)", f"{elapsed_time:.2f}"],
                    ["Total Signals Sent", self.total_sent],
                    ["Sent Approved Tickers", self.sent_approved],
                    ["Sent Rejected Tickers", self.sent_rejected],
                    ["HTTP Responses Success", self.response_success],
                    ["HTTP Responses Failed", self.response_fail],
                    ["Average Latency (ms)", f"{avg_latency * 1000:.2f}"],
                    ["App Signals Received", app_metrics.get('signals_received', 'N/A')],
                    ["App Signals Approved", app_metrics.get('signals_approved', 'N/A')],
                    ["App Signals Rejected", app_metrics.get('signals_rejected', 'N/A')],
                    ["App Forwarded Success", app_metrics.get('signals_forwarded_success', 'N/A')],
                    ["App Forwarded Error", app_metrics.get('signals_forwarded_error', 'N/A')],
                ]
                
                # Adicionar erros se houver
                if self.error_counter:
                    results.append(["", ""])
                    results.append(["Error Type", "Count"])
                    for error, count in self.error_counter.most_common():
                        results.append([error, count])
                
                with open(filename, 'w', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerows(results)
                
                self.app.log_output(f"Test results exported to: {filename}")
                messagebox.showinfo("Success", f"Test results exported successfully to:\n{filename}")
                
            except Exception as e:
                error_msg = f"Failed to export results: {e}"
                self.app.log_output(error_msg)
                messagebox.showerror("Error", error_msg)

    def _send_admin_command(self, endpoint: str, payload: Dict):
        """Envia comando administrativo para a aplicação"""
        if not self.app:
            return
        
        config = self.app.get_current_config()
        if not config:
            return
        
        url = f"http://{config['app_host']}:{config['app_port']}{endpoint}"
        
        def send_command():
            try:
                import httpx
                with httpx.Client(timeout=5.0) as client:
                    response = client.post(url, json=payload)
                    response.raise_for_status()
                    result = response.json()
                    
                    # Agendar atualização da GUI na thread principal
                    self.app.master.after(0, lambda: self.app.log_output(
                        f"Admin command successful - {endpoint}: {result.get('message', 'OK')}"
                    ))
                    
            except Exception as e:
                # Agendar atualização da GUI na thread principal
                self.app.master.after(0, lambda: self.app.log_output(
                    f"Admin command failed - {endpoint}: {e}"
                ))
        
        # Executar em thread separada para não bloquear GUI
        import threading
        threading.Thread(target=send_command, daemon=True).start()
        self.app.log_output(f"Sending admin command to {endpoint}...")

    def save_config(self):
        """Salva o perfil de teste atual em arquivo JSON"""
        if not self.app:
            return
        
        config = self.app.get_current_config()
        if not config:
            return
        
        # Remover campos internos que não devem ser salvos
        save_config = config.copy()
        save_config.pop('url', None)
        save_config.pop('app_status_url', None)
        save_config.pop('max_concurrency', None)
        
        filename = filedialog.asksavefilename(
            title="Save Test Profile",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        
        if filename:
            try:
                with open(filename, 'w') as f:
                    json.dump(save_config, f, indent=4)
                self.app.log_output(f"Test profile saved to: {filename}")
                messagebox.showinfo("Success", f"Test profile saved successfully to:\n{filename}")
            except Exception as e:
                error_msg = f"Failed to save test profile: {e}"
                self.app.log_output(error_msg)
                messagebox.showerror("Error", error_msg)

    def load_config(self):
        """Carrega um perfil de teste de arquivo JSON"""
        if not self.app:
            return
        
        filename = filedialog.askopenfilename(
            title="Load Test Profile",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        
        if filename:
            try:
                with open(filename, 'r') as f:
                    config = json.load(f)
                
                self.app.set_current_config(config)
                self.app.log_output(f"Test profile loaded from: {filename}")
                messagebox.showinfo("Success", f"Test profile loaded successfully from:\n{filename}")
            except Exception as e:
                error_msg = f"Failed to load test profile: {e}"
                self.app.log_output(error_msg)
                messagebox.showerror("Error", error_msg)

    def export_results(self):
        """Exporta os resultados do teste para arquivo CSV"""
        if self.state != "FINISHED":
            messagebox.showwarning("Warning", "No test results to export. Please run a test first.")
            return
        
        filename = filedialog.asksavefilename(
            title="Export Test Results",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        
        if filename:
            try:
                import csv
                elapsed_time = time.monotonic() - self.start_time if self.start_time > 0 else 0
                avg_latency = (sum(self.latencies) / len(self.latencies)) if self.latencies else 0
                
                app_metrics = self.last_app_status.get('signal_processing', {})
                
                # Dados do resultado
                results = [
                    ["Metric", "Value"],
                    ["Test Duration (s)", f"{elapsed_time:.2f}"],
                    ["Total Signals Sent", self.total_sent],
                    ["Sent Approved Tickers", self.sent_approved],
                    ["Sent Rejected Tickers", self.sent_rejected],
                    ["HTTP Responses Success", self.response_success],
                    ["HTTP Responses Failed", self.response_fail],
                    ["Average Latency (ms)", f"{avg_latency * 1000:.2f}"],
                    ["App Signals Received", app_metrics.get('signals_received', 'N/A')],
                    ["App Signals Approved", app_metrics.get('signals_approved', 'N/A')],
                    ["App Signals Rejected", app_metrics.get('signals_rejected', 'N/A')],
                    ["App Forwarded Success", app_metrics.get('signals_forwarded_success', 'N/A')],
                    ["App Forwarded Error", app_metrics.get('signals_forwarded_error', 'N/A')],
                ]
                
                # Adicionar erros se houver
                if self.error_counter:
                    results.append(["", ""])
                    results.append(["Error Type", "Count"])
                    for error, count in self.error_counter.most_common():
                        results.append([error, count])
                
                with open(filename, 'w', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerows(results)
                
                self.app.log_output(f"Test results exported to: {filename}")
                messagebox.showinfo("Success", f"Test results exported successfully to:\n{filename}")
                
            except Exception as e:
                error_msg = f"Failed to export results: {e}"
                self.app.log_output(error_msg)
                messagebox.showerror("Error", error_msg)

    def _send_admin_command(self, endpoint: str, payload: Dict):
        """Envia comando administrativo para a aplicação"""
        if not self.app:
            return
        
        config = self.app.get_current_config()
        if not config:
            return
        
        url = f"http://{config['app_host']}:{config['app_port']}{endpoint}"
        
        def send_command():
            try:
                import httpx
                with httpx.Client(timeout=5.0) as client:
                    response = client.post(url, json=payload)
                    response.raise_for_status()
                    result = response.json()
                    
                    # Agendar atualização da GUI na thread principal
                    self.app.master.after(0, lambda: self.app.log_output(
                        f"Admin command successful - {endpoint}: {result.get('message', 'OK')}"
                    ))
                    
            except Exception as e:
                # Agendar atualização da GUI na thread principal
                self.app.master.after(0, lambda: self.app.log_output(
                    f"Admin command failed - {endpoint}: {e}"
                ))
        
        # Executar em thread separada para não bloquear GUI
        import threading
        threading.Thread(target=send_command, daemon=True).start()
        self.app.log_output(f"Sending admin command to {endpoint}...")

# ─────────────────── GUI (The Interface) ───────────────────
class ProStressTesterApp(ttk.Frame):
    
    def __init__(self, master=None, controller=None):
        super().__init__(master, padding="10"); self.master = master; self.controller = controller
        self.master.title(APP_VERSION); self.master.geometry("1100x850")
        self.grid(sticky="nsew"); self.master.columnconfigure(0, weight=1); self.master.rowconfigure(0, weight=1)
        self.time_data = deque(maxlen=MAX_GRAPH_POINTS); self.rps_data = deque(maxlen=MAX_GRAPH_POINTS)
        self.latency_data = deque(maxlen=MAX_GRAPH_POINTS); self.queue_size_data = deque(maxlen=MAX_GRAPH_POINTS)
        self.sent_approved_data = deque(maxlen=MAX_GRAPH_POINTS); self.app_approved_data = deque(maxlen=MAX_GRAPH_POINTS)
        self._create_menu(); self._create_widgets(); self.set_control_state("IDLE")
        self.anim = animation.FuncAnimation(self.fig, self.controller.update_stats_display, interval=1000, blit=False, cache_frame_data=False)
        self.master.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _on_closing(self):
        if self.controller.state == "RUNNING":
            if messagebox.askyesno("Exit", "A test is running. Are you sure you want to exit?"):
                self.controller.stop_test(); self.master.destroy()
        else: self.master.destroy()

    def _create_menu(self):
        self.menubar = tk.Menu(self.master); self.master.config(menu=self.menubar)
        file_menu = tk.Menu(self.menubar, tearoff=0); self.file_menu = file_menu
        file_menu.add_command(label="Save Test Profile", command=self.controller.save_config)
        file_menu.add_command(label="Load Test Profile", command=self.controller.load_config)
        file_menu.add_separator(); file_menu.add_command(label="Export Results as CSV", command=self.controller.export_results, state="disabled")
        file_menu.add_separator(); file_menu.add_command(label="Exit", command=self._on_closing)
        self.menubar.add_cascade(label="File", menu=file_menu)

    def _create_widgets(self):
        self.notebook = ttk.Notebook(self); self.notebook.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1); self.columnconfigure(0, weight=1)
        self.config_tab = ttk.Frame(self.notebook, padding="10"); self.runtime_tab = ttk.Frame(self.notebook, padding="10")
        self.graphs_tab = ttk.Frame(self.notebook, padding="10"); self.admin_tab = ttk.Frame(self.notebook, padding="10")
        self.log_tab = ttk.Frame(self.notebook, padding="10")
        self.notebook.add(self.config_tab, text="Test Configuration"); self.notebook.add(self.runtime_tab, text="Live Stats & Verification")
        self.notebook.add(self.graphs_tab, text="Live Graphs"); self.notebook.add(self.admin_tab, text="App Controls")
        self.notebook.add(self.log_tab, text="Output Log")
        self._create_config_tab(); self._create_runtime_tab(); self._create_graphs_tab(); self._create_admin_tab(); self._create_log_tab()
        
    def _create_config_tab(self):
        frame = self.config_tab; frame.columnconfigure(1, weight=1)
        app_frame = ttk.LabelFrame(frame, text="Target Application", padding=10); app_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=5)
        app_frame.columnconfigure(1, weight=1)
        ttk.Label(app_frame, text="App Host:").grid(row=0, column=0, sticky="w", padx=5)
        self.app_host_entry = ttk.Entry(app_frame); self.app_host_entry.insert(0, "localhost")
        self.app_host_entry.grid(row=0, column=1, sticky="ew")
        ttk.Label(app_frame, text="App Port:").grid(row=0, column=2, sticky="w", padx=5)
        self.app_port_entry = ttk.Entry(app_frame, width=10); self.app_port_entry.insert(0, "80")
        self.app_port_entry.grid(row=0, column=3, sticky="w")
        
        ticker_pane = ttk.PanedWindow(frame, orient=tk.HORIZONTAL); ticker_pane.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=5)
        def create_ticker_frame(parent, title, default_tickers):
            f = ttk.LabelFrame(parent, text=title, padding=10); f.columnconfigure(0, weight=1); f.rowconfigure(0, weight=1)
            tree = ttk.Treeview(f, columns=("ticker",), show="headings", height=5); tree.heading("ticker", text="Ticker"); tree.grid(row=0, column=0, sticky="nsew")
            for t in default_tickers: tree.insert("", "end", values=(t,))
            buttons = ttk.Frame(f); buttons.grid(row=0, column=1, sticky="ns", padx=5)
            ttk.Button(buttons, text="Add", command=lambda tr=tree: self._add_ticker_item(tr)).pack(fill="x", pady=2)
            ttk.Button(buttons, text="Remove", command=lambda tr=tree: self._remove_ticker_item(tr)).pack(fill="x", pady=2)
            return f, tree
        approved_frame, self.approved_tree = create_ticker_frame(ticker_pane, "Tickers to be APPROVED", ["AAPL", "MSFT", "GOOG", "AMZN", "NVDA"])
        rejected_frame, self.rejected_tree = create_ticker_frame(ticker_pane, "Tickers to be REJECTED", ["XYZ", "TEST", "REJECT", "FAIL", "JUNK"])
        ticker_pane.add(approved_frame, weight=1); ticker_pane.add(rejected_frame, weight=1)

        payload_pane = ttk.PanedWindow(frame, orient=tk.HORIZONTAL); payload_pane.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=5)
        frame.rowconfigure(2, weight=1)
        payload_frame = ttk.LabelFrame(payload_pane, text="Request Body Template (use {ticker} and {faker...})", padding=10)
        self.payload_text = ScrolledText(payload_frame, height=10, wrap=tk.WORD); self.payload_text.insert("1.0", DEFAULT_PAYLOAD_TEMPLATE); self.payload_text.pack(fill="both", expand=True)
        headers_frame = ttk.LabelFrame(payload_pane, text="Custom Headers (JSON format)", padding=10)
        self.headers_text = ScrolledText(headers_frame, height=10, wrap=tk.WORD); self.headers_text.insert("1.0", '{\n    "Content-Type": "application/json"\n}'); self.headers_text.pack(fill="both", expand=True)
        payload_pane.add(payload_frame, weight=1); payload_pane.add(headers_frame, weight=1)
    
    def _add_ticker_item(self, tree):
        value = simpledialog.askstring("Add Ticker", "Enter the ticker symbol:")
        if value: tree.insert("", "end", values=(value.upper(),))
    def _remove_ticker_item(self, tree):
        if not tree.selection(): messagebox.showinfo("Information", "Please select a ticker to remove.")
        else:
            for item_id in tree.selection(): tree.delete(item_id)
            
    def _create_runtime_tab(self):
        frame = self.runtime_tab; frame.columnconfigure(0, weight=1)
        top_pane = ttk.PanedWindow(frame, orient=tk.HORIZONTAL); top_pane.grid(row=0, column=0, sticky="ew", pady=5)
        ctrl_frame = ttk.LabelFrame(top_pane, text="Test Controls", padding=10); ctrl_frame.columnconfigure(1, weight=1)
        def create_slider(parent, text, from_, to, default, row, config_key=None):
            ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", padx=5)
            slider = ttk.Scale(parent, from_=from_, to=to, orient=tk.HORIZONTAL); slider.set(default); slider.grid(row=row, column=1, sticky="ew", padx=5)
            label = ttk.Label(parent, text=str(default), width=5); label.grid(row=row, column=2, sticky="w", padx=5)
            
            def on_slider_change(value):
                # Atualizar label
                int_value = int(float(value))
                label.config(text=str(int_value))
                
                # Se tem config_key e teste está rodando, enviar update em tempo real
                if config_key and self.controller.state == "RUNNING":
                    self.controller.update_config_realtime({config_key: int_value})
            
            slider.config(command=on_slider_change)
            return slider
        self.rps_slider = create_slider(ctrl_frame, "Target RPS:", 1, 1000, 100, 0, "rps")
        self.ratio_slider = create_slider(ctrl_frame, "Approved Ratio (%):", 0, 100, 80, 1, "approved_ratio")
        self.ramp_up_slider = create_slider(ctrl_frame, "Ramp-up (s):", 0, 120, 10, 2, "ramp_up_s")
        self.duration_slider = create_slider(ctrl_frame, "Duration (s):", 0, 3600, 60, 3)
        action_frame = ttk.Frame(ctrl_frame); action_frame.grid(row=4, column=0, columnspan=3, pady=10)
        self.start_button = ttk.Button(action_frame, text="Start Test", command=self.controller.start_test, style="Accent.TButton"); self.start_button.pack(side="left", padx=10)
        self.stop_button = ttk.Button(action_frame, text="Stop Test", command=self.controller.stop_test); self.stop_button.pack(side="left", padx=10)
        top_pane.add(ctrl_frame, weight=1)
        stats_pane = ttk.PanedWindow(frame, orient=tk.VERTICAL); stats_pane.grid(row=1, column=0, sticky="nsew", pady=10); frame.rowconfigure(1, weight=1)
        tester_stats_frame = ttk.LabelFrame(stats_pane, text="Tester Stats (Client-Side)", padding=10)
        app_stats_frame = ttk.LabelFrame(stats_pane, text="Application Stats (Polled from App)", padding=10)
        verify_stats_frame = ttk.LabelFrame(stats_pane, text="Test Verification", padding=10)
        stats_pane.add(tester_stats_frame, weight=1); stats_pane.add(app_stats_frame, weight=1); stats_pane.add(verify_stats_frame, weight=1)
        self.live_stats_vars = {}; self.app_status_labels = {}
        def create_stat_label(parent, text, key, row, col, var_dict):
            ttk.Label(parent, text=text).grid(row=row, column=col, sticky="w", padx=5, pady=2)
            var = tk.StringVar(value="0")
            label = ttk.Label(parent, textvariable=var, font="-weight bold")
            label.grid(row=row, column=col+1, sticky="w", padx=5)
            var_dict[key] = (var, label)
            return var, label
        create_stat_label(tester_stats_frame, "Target RPS:", "rps", 0, 0, self.live_stats_vars)
        create_stat_label(tester_stats_frame, "Total Sent:", "total_sent", 1, 0, self.live_stats_vars)
        create_stat_label(tester_stats_frame, "Avg Latency:", "avg_lat", 2, 0, self.live_stats_vars)
        create_stat_label(tester_stats_frame, "Sent 'Approved' Tickers:", "sent_approved", 0, 2, self.live_stats_vars)
        create_stat_label(tester_stats_frame, "Sent 'Rejected' Tickers:", "sent_rejected", 1, 2, self.live_stats_vars)
        create_stat_label(tester_stats_frame, "Responses OK:", "resp_success", 0, 4, self.live_stats_vars)
        create_stat_label(tester_stats_frame, "Responses Error:", "resp_fail", 1, 4, self.live_stats_vars)
        create_stat_label(app_stats_frame, "App Signals Received:", "app_received", 0, 0, self.app_status_labels)
        create_stat_label(app_stats_frame, "App Signals Approved:", "app_approved", 1, 0, self.app_status_labels)
        create_stat_label(app_stats_frame, "App Signals Rejected:", "app_rejected", 2, 0, self.app_status_labels)
        create_stat_label(app_stats_frame, "App Forwarded Success:", "app_fwd_ok", 0, 2, self.app_status_labels)
        create_stat_label(app_stats_frame, "App Forwarded Error:", "app_fwd_err", 1, 2, self.app_status_labels)
        create_stat_label(app_stats_frame, "Queue Size:", "queue_size", 0, 4, self.app_status_labels)
        create_stat_label(app_stats_frame, "Finviz Engine:", "engine_status", 1, 4, self.app_status_labels)
        create_stat_label(app_stats_frame, "Webhook Limiter:", "webhook_limiter", 2, 4, self.app_status_labels)
        for var, _ in self.app_status_labels.values(): var.set("N/A")
        create_stat_label(verify_stats_frame, "Approved Mismatch:", "mismatch_approved", 0, 0, self.live_stats_vars)
        create_stat_label(verify_stats_frame, "Rejected Mismatch:", "mismatch_rejected", 1, 0, self.live_stats_vars)
        
    def _create_graphs_tab(self):
        frame = self.graphs_tab; frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)
        self.fig = Figure(figsize=(9, 7), dpi=100)
        self.ax1 = self.fig.add_subplot(2, 2, 1); self.ax2 = self.fig.add_subplot(2, 2, 2); self.ax3 = self.fig.add_subplot(2, 1, 2)
        self.canvas = FigureCanvasTkAgg(self.fig, master=frame); self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        self.clear_graphs()

    def _create_admin_tab(self):
        frame = self.admin_tab; frame.columnconfigure(0, weight=1)
        token_frame = ttk.LabelFrame(frame, text="Admin Token", padding=10); token_frame.grid(row=0, column=0, sticky="ew", pady=5); token_frame.columnconfigure(1, weight=1)
        ttk.Label(token_frame, text="Token:").grid(row=0, column=0, sticky="w", padx=5)
        self.admin_token_entry = ttk.Entry(token_frame); self.admin_token_entry.insert(0, "your-secret-token"); self.admin_token_entry.grid(row=0, column=1, sticky="ew")
        engine_frame = ttk.LabelFrame(frame, text="Finviz Engine Control", padding=10); engine_frame.grid(row=1, column=0, sticky="ew", pady=5)
        ttk.Button(engine_frame, text="Pause Engine", command=lambda: self.controller._send_admin_command('/admin/engine/pause', {"token": self.admin_token_entry.get()})).pack(side="left", padx=5)
        ttk.Button(engine_frame, text="Resume Engine", command=lambda: self.controller._send_admin_command('/admin/engine/resume', {"token": self.admin_token_entry.get()})).pack(side="left", padx=5)
        ttk.Button(engine_frame, text="Manual Refresh", command=lambda: self.controller._send_admin_command('/admin/engine/manual-refresh', {"token": self.admin_token_entry.get()})).pack(side="left", padx=5)
        config_frame = ttk.LabelFrame(frame, text="Update Finviz Config", padding=10); config_frame.grid(row=2, column=0, sticky="ew", pady=5); config_frame.columnconfigure(1, weight=1)
        ttk.Label(config_frame, text="New Top-N:").grid(row=0, column=0, sticky="w"); self.new_top_n_entry = ttk.Entry(config_frame, width=10); self.new_top_n_entry.grid(row=0, column=1, sticky="w")
        ttk.Label(config_frame, text="New Refresh (s):").grid(row=1, column=0, sticky="w"); self.new_refresh_entry = ttk.Entry(config_frame, width=10); self.new_refresh_entry.grid(row=1, column=1, sticky="w")
        def update_config_cmd():
            payload = {"token": self.admin_token_entry.get()}
            if self.new_top_n_entry.get(): payload['top_n'] = int(self.new_top_n_entry.get())
            if self.new_refresh_entry.get(): payload['refresh'] = int(self.new_refresh_entry.get())
            self.controller._send_admin_command('/finviz/config', payload)
        ttk.Button(config_frame, text="Update Config", command=update_config_cmd).grid(row=2, column=0, columnspan=2, pady=5)
        metrics_frame = ttk.LabelFrame(frame, text="Metrics Control", padding=10); metrics_frame.grid(row=3, column=0, sticky="ew", pady=5)
        ttk.Button(metrics_frame, text="Reset App Metrics", command=lambda: self.controller._send_admin_command('/admin/metrics/reset', {"token": self.admin_token_entry.get()})).pack(padx=5)

    def _create_log_tab(self):
        frame = self.log_tab; frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)
        self.output_text = ScrolledText(frame, state='disabled', wrap=tk.WORD, height=15); self.output_text.grid(row=0, column=0, sticky="nsew")

    def get_current_config(self) -> Optional[Dict]:
        try:
            config = {'app_host': self.app_host_entry.get(), 'app_port': int(self.app_port_entry.get())}
            config['url'] = f"http://{config['app_host']}:{config['app_port']}/webhook/in"
            config['app_status_url'] = f"http://{config['app_host']}:{config['app_port']}/admin/system-info"
            config.update({ 'rps': int(self.rps_slider.get()), 'approved_ratio': int(self.ratio_slider.get()), 'ramp_up_s': int(self.ramp_up_slider.get()), 'duration_s': int(self.duration_slider.get()),
                            'approved_tickers': [self.approved_tree.item(i)['values'][0] for i in self.approved_tree.get_children()],
                            'rejected_tickers': [self.rejected_tree.item(i)['values'][0] for i in self.rejected_tree.get_children()],
                            'payload_template': self.payload_text.get("1.0", tk.END), 'headers': json.loads(self.headers_text.get("1.0", tk.END)), 'max_concurrency': 500})
            if not config['approved_tickers'] and not config['rejected_tickers']: raise ValueError("At least one ticker is required.")
            return config
        except Exception as e:
            messagebox.showerror("Configuration Error", f"Invalid configuration: {e}"); return None
    
    def set_current_config(self, config: Dict):
        self.app_host_entry.delete(0, tk.END); self.app_host_entry.insert(0, config.get('app_host', 'localhost'))
        self.app_port_entry.delete(0, tk.END); self.app_port_entry.insert(0, config.get('app_port', 80))
        self.rps_slider.set(config.get('rps', 100)); self.ratio_slider.set(config.get('approved_ratio', 80))
        self.ramp_up_slider.set(config.get('ramp_up_s', 10)); self.duration_slider.set(config.get('duration_s', 60))
        self.approved_tree.delete(*self.approved_tree.get_children())
        for t in config.get('approved_tickers', []): self.approved_tree.insert("", "end", values=(t,))
        self.rejected_tree.delete(*self.rejected_tree.get_children())
        for t in config.get('rejected_tickers', []): self.rejected_tree.insert("", "end", values=(t,))
        self.payload_text.delete("1.0", tk.END); self.payload_text.insert("1.0", config.get('payload_template', DEFAULT_PAYLOAD_TEMPLATE))
        self.headers_text.delete("1.0", tk.END); self.headers_text.insert("1.0", json.dumps(config.get('headers', {}), indent=4))
        self.log_output("UI updated with loaded configuration.")

    def set_control_state(self, state: str):
        is_idle = state in ["IDLE", "FINISHED"]
        is_running = state == "RUNNING"
        is_stopping = state == "STOPPING"
        
        self.start_button.config(state=tk.NORMAL if is_idle else tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL if (is_running or is_stopping) else tk.DISABLED)
        
        # Durante STOPPING, manter botão Stop ativo mas desabilitado visualmente
        if is_stopping:
            self.stop_button.config(text="Stopping...", state=tk.DISABLED)
        else:
            self.stop_button.config(text="Stop Test", state=tk.NORMAL if is_running else tk.DISABLED)
        
        # Disable apenas a aba de configuração durante teste
        for tab_frame in [self.config_tab]:
             for widget in tab_frame.winfo_children():
                self._set_state_recursive(widget, tk.NORMAL if is_idle else tk.DISABLED)
        
        # Manter os Test Controls sliders ativos durante o teste para alterações em tempo real
        if is_running:
            # Os sliders já têm callbacks configurados para atualização em tempo real
            self.rps_slider.config(state=tk.NORMAL)
            self.ratio_slider.config(state=tk.NORMAL)
            self.ramp_up_slider.config(state=tk.NORMAL)
            # Duration slider permanece desabilitado durante teste (não pode ser alterado em tempo real)
            self.duration_slider.config(state=tk.DISABLED)
        elif is_idle:
            # Quando o teste não está rodando, todos os sliders devem estar habilitados
            self.rps_slider.config(state=tk.NORMAL)
            self.ratio_slider.config(state=tk.NORMAL)
            self.ramp_up_slider.config(state=tk.NORMAL)
            self.duration_slider.config(state=tk.NORMAL)
        
        self.file_menu.entryconfig("Save Test Profile", state=tk.NORMAL if is_idle else tk.DISABLED)
        self.file_menu.entryconfig("Load Test Profile", state=tk.NORMAL if is_idle else tk.DISABLED)
        self.file_menu.entryconfig("Export Results as CSV", state=tk.NORMAL if state == "FINISHED" else tk.DISABLED)

    def _set_state_recursive(self, widget, state):
        try:
            if 'state' in widget.config(): widget.config(state=state)
        except tk.TclError: pass
        for child in widget.winfo_children(): self._set_state_recursive(child, state)
            
    def log_output(self, message: str):
        self.output_text.config(state='normal')
        self.output_text.insert(tk.END, f"{time.strftime('%H:%M:%S')} - {message}\n")
        self.output_text.see(tk.END); self.output_text.config(state='disabled')
    
    def clear_output_log(self):
        self.output_text.config(state='normal')
        self.output_text.delete('1.0', tk.END); self.output_text.config(state='disabled')

    def update_live_stats(self, stats: Dict):
        for key, (var, label) in self.live_stats_vars.items():
            value = stats.get(key)
            if value is not None:
                color = ''
                if "mismatch" in key and isinstance(value, int) and value != 0:
                    var.set(f"⚠️ {value}"); color = 'red'
                else: var.set(str(value))
                label.config(foreground=color)

    def update_app_status_display(self, status: Dict):
        if "error" in status:
            for var, _ in self.app_status_labels.values(): var.set("POLL FAILED")
            self.app_status_labels['engine_status'][0].set(status['error']); return
        metrics = status.get('signal_processing', {}); queue_info = status.get('queue', {})
        engine_info = status.get('engine', {}); webhook_info = status.get('webhook_rate_limiter', {})
        self.app_status_labels['app_received'][0].set(metrics.get('signals_received', 'N/A'))
        self.app_status_labels['app_approved'][0].set(metrics.get('signals_approved', 'N/A'))
        self.app_status_labels['app_rejected'][0].set(metrics.get('signals_rejected', 'N/A'))
        self.app_status_labels['app_fwd_ok'][0].set(metrics.get('signals_forwarded_success', 'N/A'))
        self.app_status_labels['app_fwd_err'][0].set(metrics.get('signals_forwarded_error', 'N/A'))
        self.app_status_labels['queue_size'][0].set(queue_info.get('current_size', 'N/A'))
        engine_status = "Paused" if engine_info.get('is_paused') else "Running" if engine_info.get('is_running') else "Stopped"
        self.app_status_labels['engine_status'][0].set(f"{engine_status} ({engine_info.get('last_update_status', 'N/A')})")
        webhook_status = "Enabled" if webhook_info.get('rate_limiting_enabled') else "Disabled"
        self.app_status_labels['webhook_limiter'][0].set(f"{webhook_status} ({webhook_info.get('tokens_available', 'N/A')} tokens)")
    
    def clear_graphs(self):
        self.time_data.clear(); self.rps_data.clear(); self.latency_data.clear(); self.queue_size_data.clear()
        self.sent_approved_data.clear(); self.app_approved_data.clear();
        for ax in [self.ax1, self.ax2, self.ax3]: ax.clear()
        if hasattr(self, 'ax1_twin'): self.ax1_twin.clear()
        
        self.ax1.set_title("RPS & Latency"); self.ax1.set_ylabel("RPS", color='tab:blue'); self.ax1.tick_params(axis='y', labelcolor='tab:blue'); self.ax1.grid(True, linestyle='--', alpha=0.6)
        if not hasattr(self, 'ax1_twin'): self.ax1_twin = self.ax1.twinx()
        self.ax1_twin.set_ylabel("Avg Latency (ms)", color='tab:orange'); self.ax1_twin.tick_params(axis='y', labelcolor='tab:orange')
        self.ax2.set_title("App Queue Size"); self.ax2.set_ylabel("Signals in Queue", color='tab:purple'); self.ax2.tick_params(axis='y', labelcolor='tab:purple'); self.ax2.grid(True, linestyle='--', alpha=0.6)
        self.ax3.set_title("Verification: Sent vs. App Approved (Cumulative)"); self.ax3.set_xlabel("Time (s)"); self.ax3.set_ylabel("Total Count"); self.ax3.grid(True)
        self.fig.tight_layout(); self.canvas.draw()
    
    def add_graph_data(self, t, rps, latency_ms, queue_size, sent_approved, app_approved):
        self.time_data.append(t); self.rps_data.append(rps); self.latency_data.append(latency_ms)
        self.queue_size_data.append(queue_size); self.sent_approved_data.append(sent_approved); self.app_approved_data.append(app_approved)
        self.ax1.clear(); self.ax1_twin.clear(); self.ax1.plot(self.time_data, self.rps_data, color='tab:blue', label='RPS'); self.ax1_twin.plot(self.time_data, self.latency_data, color='tab:orange', label='Latency (ms)')
        self.ax1.set_ylabel("RPS", color='tab:blue'); self.ax1_twin.set_ylabel("Avg Latency (ms)", color='tab:orange'); self.ax1.grid(True, linestyle='--', alpha=0.6)
        self.ax2.clear(); self.ax2.plot(self.time_data, self.queue_size_data, color='tab:purple'); self.ax2.set_ylabel("Signals in Queue", color='tab:purple'); self.ax2.grid(True, linestyle='--', alpha=0.6)
        self.ax3.clear(); self.ax3.plot(self.time_data, self.sent_approved_data, color='cyan', label='Tester Sent "Approved"', linestyle='--'); self.ax3.plot(self.time_data, self.app_approved_data, color='lime', label='App Processed "Approved"')
        self.ax3.set_xlabel("Time (s)"); self.ax3.set_ylabel("Total Count"); self.ax3.legend(loc='upper left', fontsize='small'); self.ax3.grid(True)
        self.fig.tight_layout(); self.canvas.draw()

# ─────────────────── MAIN & CLI ───────────────────
def run_headless(args):
    print(f"--- {APP_VERSION} Headless Mode ---");
    try:
        with open(args.config, 'r') as f: config = json.load(f)
        print(f"Loaded configuration from {args.config}")
    except Exception as e: print(f"Error: Could not load config file '{args.config}': {e}", file=sys.stderr); sys.exit(1)
    config['app_host'] = args.host or config.get('app_host', 'localhost'); config['app_port'] = args.port or config.get('app_port', 80)
    config['url'] = f"http://{config['app_host']}:{config['app_port']}/webhook/in"; config['app_status_url'] = f"http://{config['app_host']}:{config['app_port']}/admin/system-info"
    config['rps'] = args.rps or config.get('rps', 100); config['duration_s'] = args.duration or config.get('duration_s', 60)
    output_queue = queue.Queue(); shutdown_event = threading.Event()
    worker = AsyncWorker(config, output_queue, shutdown_event); worker_thread = threading.Thread(target=worker.run, daemon=True); worker_thread.start()
    poller = AppStatusPoller(config, output_queue, shutdown_event); poller_thread = threading.Thread(target=poller.run, daemon=True); poller_thread.start()
    print(f"Starting test against {config['url']} for {config['duration_s']} seconds at {config['rps']} RPS.")
    start_time = time.monotonic()
    sent_approved, sent_rejected, resp_ok, resp_err, total_sent = 0, 0, 0, 0, 0; last_app_status = {}
    try:
        while time.monotonic() - start_time < config['duration_s']: time.sleep(1)
        print("\nTest duration reached. Shutting down...")
    except KeyboardInterrupt: print("\nInterrupted by user. Shutting down...")
    finally:
        shutdown_event.set(); worker_thread.join(timeout=5); poller_thread.join(timeout=3)
    while not output_queue.empty():
        msg = output_queue.get_nowait()
        if msg['type'] == 'sent_result':
            total_sent += 1
            if msg['payload']['was_approved_ticker']: sent_approved += 1
            else: sent_rejected += 1
            if 200 <= msg['payload']['status_code'] < 300: resp_ok += 1
            else: resp_err += 1
        elif msg['type'] == 'app_status': last_app_status = msg['payload']
    print("\n--- FINAL RESULTS ---");
    if total_sent == 0: print("No requests were sent."); return
    print(f"\n[Tester Stats]"); print(f"  Total Signals Sent: {total_sent}"); print(f"    - 'Approved' Tickers: {sent_approved}"); print(f"    - 'Rejected' Tickers: {sent_rejected}")
    print(f"  HTTP Responses OK: {resp_ok} ({resp_ok/total_sent*100:.2f}%)"); print(f"  HTTP Responses Error: {resp_err} ({resp_err/total_sent*100:.2f}%)")
    print(f"\n[Application Stats (Last Poll)]"); app_metrics = last_app_status.get('signal_processing', {}); app_approved = app_metrics.get('signals_approved', 0); app_rejected = app_metrics.get('signals_rejected', 0)
    print(f"  App Signals Received: {app_metrics.get('signals_received', 0)}"); print(f"  App Signals Approved: {app_approved}"); print(f"  App Signals Rejected: {app_rejected}")
    print(f"\n[Verification]"); print(f"  Approved Mismatch: {sent_approved - app_approved}"); print(f"  Rejected Mismatch: {sent_rejected - app_rejected}")

def main():
    parser = argparse.ArgumentParser(description=f"{APP_VERSION} - A GUI/CLI test tool for the Trading-Signal-Processor.")
    parser.add_argument('--cli', action='store_true', help='Run in headless (command-line) mode.')
    parser.add_argument('-c', '--config', type=str, help='Path to a JSON configuration file (required for CLI).')
    parser.add_argument('--host', type=str, help='Override app host from config file.'); parser.add_argument('--port', type=int, help='Override app port from config file.')
    parser.add_argument('--rps', type=int, help='Override RPS from config file.'); parser.add_argument('--duration', type=int, help='Override test duration (seconds) from config file.')
    args = parser.parse_args()
    if args.cli:
        if not args.config: parser.error("--config file is required for CLI mode.")
        run_headless(args)
    else:
        root = tk.Tk(); style = ttk.Style(root)
        try:
            if sys.platform == "win32": style.theme_use('winnative')
            elif sys.platform == "darwin": style.theme_use('aqua')
            else: style.theme_use('clam')
        except tk.TclError: pass
        style.configure('Accent.TButton', font='-weight bold'); style.configure('Mismatched.TLabel', foreground='red', font='-weight bold')
        controller = TestController(None); app = ProStressTesterApp(master=root, controller=controller)
        controller.app = app; app.mainloop()

if __name__ == "__main__":
    main()
