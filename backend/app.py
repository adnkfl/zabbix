from flask import Flask, jsonify, request, send_file
import requests
from flask_cors import CORS
import re
import edge_tts
import asyncio
import uuid
import os
import time
import json
import threading
from google.cloud import texttospeech
import io
import warnings
from urllib3.exceptions import InsecureRequestWarning

warnings.simplefilter('ignore', InsecureRequestWarning)

app = Flask(__name__)
CORS(app)

ZABBIX_URL = 'https://zabbix.localhost/api_jsonrpc.php'
ZABBIX_API_TOKEN = 'YYYYYYYYYYYYYYYYYYYYYXXXXXXXXXXXXXXXXXXXXYYYYYYYYYYYYYYYYYYY'
ZABBIX_VERIFY_SSL = False

HOSTS_CACHE = None
HOSTS_CACHE_TIME = 0
HOSTS_CACHE_TTL = 300

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "optimal-iris-462311-n4-YYYYYYYY.json"

# Arka planda Zabbix'ten host verisini düzenli çekip cache'e yazan fonksiyon
def poll_hosts_background():
    import time
    global HOSTS_CACHE, HOSTS_CACHE_TIME
    while True:
        try:
            with app.app_context():
                payload = {
                    "jsonrpc": "2.0",
                    "method": "host.get",
                    "params": {
                        "output": ["hostid", "host", "name", "status"],
                        "selectGroups": ["name"],
                        "selectTags": "extend",
                        "selectInventory": ["model", "vendor", "hardware", "hardware_full", "os"]
                    },
                    "id": 1
                }
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {ZABBIX_API_TOKEN}"
                }
                response = requests.post(ZABBIX_URL, json=payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                data = response.json()
                hosts = data.get('result', [])
                for host in hosts:
                    iface_payload = {
                        "jsonrpc": "2.0",
                        "method": "hostinterface.get",
                        "params": {
                            "output": ["interfaceid", "hostid", "available", "type", "ip"],
                            "hostids": host["hostid"]
                        },
                        "id": 2
                    }
                    iface_resp = requests.post(ZABBIX_URL, json=iface_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                    iface_data = iface_resp.json()
                    interfaces = iface_data.get('result', [])
                    host['available'] = '0'
                    host['interface_type'] = None
                    host['ip'] = None
                    for iface in interfaces:
                        if iface.get('type') == '1':
                            host['available'] = iface.get('available', '0')
                            host['interface_type'] = iface.get('type')
                            host['ip'] = iface.get('ip')
                            break
                    else:
                        if interfaces:
                            host['available'] = interfaces[0].get('available', '0')
                            host['interface_type'] = interfaces[0].get('type')
                            host['ip'] = interfaces[0].get('ip')
                    inventory = host.get('inventory') or {}
                    host['model'] = inventory.get('model') or inventory.get('hardware') or inventory.get('hardware_full')
                    host['vendor'] = inventory.get('vendor')
                    host['os'] = inventory.get('os', '')
                    host['uptime'] = '-'
                    # Uptime bilgisini sırayla farklı anahtarlarla dene
                    uptime_keys = [
                        "system.uptime",
                        "hp.server.hw.uptime[hrSystemUptime]",
                        "system.net.uptime[sysUpTime.0]"
                    ]
                    for key in uptime_keys:
                        uptime_payload = {
                            "jsonrpc": "2.0",
                            "method": "item.get",
                            "params": {
                                "output": ["lastvalue"],
                                "hostids": host["hostid"],
                                "search": {"key_": key},
                                "sortfield": "itemid",
                                "sortorder": "ASC",
                                "limit": 1
                            },
                            "id": 3
                        }
                        uptime_resp = requests.post(ZABBIX_URL, json=uptime_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                        uptime_data = uptime_resp.json()
                        uptime_items = uptime_data.get('result', [])
                        if uptime_items and uptime_items[0].get('lastvalue'):
                            host['uptime'] = uptime_items[0]['lastvalue']
                            break
                    # CPU değerini çek
                    cpu_keys = [
                        "system.cpu.util[,idle]",
                        "system.cpu.util[,user]",
                        "system.cpu.util"
                    ]
                    host['cpu'] = '-'
                    for key in cpu_keys:
                        cpu_payload = {
                            "jsonrpc": "2.0",
                            "method": "item.get",
                            "params": {
                                "output": ["lastvalue"],
                                "hostids": host["hostid"],
                                "search": {"key_": key},
                                "sortfield": "itemid",
                                "sortorder": "ASC",
                                "limit": 1
                            },
                            "id": 4
                        }
                        cpu_resp = requests.post(ZABBIX_URL, json=cpu_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                        cpu_data = cpu_resp.json()
                        cpu_items = cpu_data.get('result', [])
                        if cpu_items and cpu_items[0].get('lastvalue'):
                            try:
                                val = float(cpu_items[0]['lastvalue'])
                                if key == "system.cpu.util[,idle]":
                                    host['cpu'] = f"{100 - val:.1f}"
                                else:
                                    host['cpu'] = f"{val:.1f}"
                            except:
                                host['cpu'] = cpu_items[0]['lastvalue']
                            break
                    # MEM değerini çek
                    mem_keys_percent = [
                        "memory.utilization", "mem.utilization", "memory.usage", "mem.usage", "vm.memory.util[vm.memory.util.1]"
                    ]
                    mem_percent = None
                    for key in mem_keys_percent:
                        mem_payload = {
                            "jsonrpc": "2.0",
                            "method": "item.get",
                            "params": {
                                "output": ["lastvalue"],
                                "hostids": host["hostid"],
                                "search": {"key_": key},
                                "sortfield": "itemid",
                                "sortorder": "ASC",
                                "limit": 1
                            },
                            "id": 8
                        }
                        mem_resp = requests.post(ZABBIX_URL, json=mem_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                        mem_data = mem_resp.json()
                        mem_items = mem_data.get('result', [])
                        if mem_items and mem_items[0].get('lastvalue'):
                            try:
                                mem_percent = float(mem_items[0]['lastvalue'])
                                break
                            except:
                                pass
                    if mem_percent is None:
                        mem_used = None
                        mem_total = None
                        mem_available = None
                        mem_keys_used = [
                            "vm.memory.size[used]", "system.memory.used", "system.mem.used", "memUsedReal", "hrStorageUsed"
                        ]
                        mem_keys_total = [
                            "vm.memory.size[total]", "system.memory.total", "system.mem.total", "memTotalReal", "hrStorageSize"
                        ]
                        mem_keys_available = [
                            "vm.memory.size[available]", "vm.memory.size[free]", "system.memory.free", "system.mem.free", "memAvailReal"
                        ]
                        for key in mem_keys_used:
                            mem_payload = {
                                "jsonrpc": "2.0",
                                "method": "item.get",
                                "params": {
                                    "output": ["lastvalue"],
                                    "hostids": host["hostid"],
                                    "search": {"key_": key},
                                    "sortfield": "itemid",
                                    "sortorder": "ASC",
                                    "limit": 1
                                },
                                "id": 5
                            }
                            mem_resp = requests.post(ZABBIX_URL, json=mem_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                            mem_data = mem_resp.json()
                            mem_items = mem_data.get('result', [])
                            if mem_items and mem_items[0].get('lastvalue'):
                                try:
                                    mem_used = float(mem_items[0]['lastvalue'])
                                    break
                                except:
                                    pass
                        for key in mem_keys_total:
                            mem_payload = {
                                "jsonrpc": "2.0",
                                "method": "item.get",
                                "params": {
                                    "output": ["lastvalue"],
                                    "hostids": host["hostid"],
                                    "search": {"key_": key},
                                    "sortfield": "itemid",
                                    "sortorder": "ASC",
                                    "limit": 1
                                },
                                "id": 6
                            }
                            mem_resp = requests.post(ZABBIX_URL, json=mem_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                            mem_data = mem_resp.json()
                            mem_items = mem_data.get('result', [])
                            if mem_items and mem_items[0].get('lastvalue'):
                                try:
                                    mem_total = float(mem_items[0]['lastvalue'])
                                    break
                                except:
                                    pass
                        for key in mem_keys_available:
                            mem_payload = {
                                "jsonrpc": "2.0",
                                "method": "item.get",
                                "params": {
                                    "output": ["lastvalue"],
                                    "hostids": host["hostid"],
                                    "search": {"key_": key},
                                    "sortfield": "itemid",
                                    "sortorder": "ASC",
                                    "limit": 1
                                },
                                "id": 7
                            }
                            mem_resp = requests.post(ZABBIX_URL, json=mem_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                            mem_data = mem_resp.json()
                            mem_items = mem_data.get('result', [])
                            if mem_items and mem_items[0].get('lastvalue'):
                                try:
                                    mem_available = float(mem_items[0]['lastvalue'])
                                    break
                                except:
                                    pass
                        if mem_used is not None and mem_total and mem_total > 0:
                            mem_percent = 100 * mem_used / mem_total
                        elif mem_total and mem_available is not None and mem_total > 0:
                            mem_percent = 100 * (mem_total - mem_available) / mem_total
                    if mem_percent is not None:
                        host['mem'] = f"{mem_percent:.1f}"
                    else:
                        host['mem'] = '-'
                HOSTS_CACHE = hosts
                HOSTS_CACHE_TIME = time.time()
        except Exception as e:
            print('Arka plan polling hatası:', e)
        time.sleep(120)  # 2 dakikada bir güncelle

@app.route('/api/hosts', methods=['GET'])
def get_hosts():
    global HOSTS_CACHE, HOSTS_CACHE_TIME
    if HOSTS_CACHE is not None:
        return jsonify(HOSTS_CACHE)
    else:
        return jsonify([])

def google_tts(text, lang="tr-TR"):
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code=lang,
        ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3
    )
    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )
    return response.audio_content

@app.route('/api/tts2', methods=['POST'])
def tts2():
    data = request.get_json()
    text = data.get('text', '')
    if not text:
        return {"error": "text parametresi zorunlu"}, 400
    audio_content = google_tts(text)
    return send_file(
        io.BytesIO(audio_content),
        mimetype='audio/mpeg',
        as_attachment=False,
        download_name='tts2.mp3'
    )

@app.route('/api/ask', methods=['POST'])
def ask_zabbix():
    # Pattern listeleri eksiksiz ve doğru sırada
    network_patterns = [
        r"ağ cihazı", r"network cihaz", r"network device", r"switch", r"router"
    ]
    snmp_patterns = [
        r"snmp"
    ]
    agent_patterns = [
        r"agent"
    ]
    all_patterns = [
        r"host", r"cihaz", r"sunucu"
    ]
    list_patterns = [
        r"liste", r"isim", r"ad"
    ]
    aktif_patterns = [
        r"erişilebilir", r"aktif", r"up"
    ]
    pasif_patterns = [
        r"erişilemez", r"pasif", r"down"
    ]
    # Toplam host/cihaz/host adedi/host sayısı/kaç host/kaç cihaz gibi sorular için ortak blok
    import re
    host_count_patterns = [
        r"toplam host say(ı|isi|ısı|ısı)? nedir",  # toplam host sayısı nedir
        r"toplam host say(ı|isi|ısı|ısı)? kaç",    # toplam host sayısı kaç
        r"kaç host vardır",                        # kaç host vardır
        r"toplam host bulunmaktadır",              # toplam host bulunmaktadır
        r"toplam host kaç tanedir",                # toplam host kaç tanedir
        r"toplam host adedi",                      # toplam host adedi
        r"host adedi",                             # host adedi
        r"host say(ı|isi|ısı|ısı)?",               # host sayısı
        r"kaç host var",                           # kaç host var
        r"kaç adet host var",                      # kaç adet host var
        r"kaç tane host var",                      # kaç tane host var
        r"toplam cihaz adedi",                     # toplam cihaz adedi
        r"kaç cihaz vardır",                       # kaç cihaz vardır
        r"toplam cihaz say(ı|isi|ısı|ısı)?",       # toplam cihaz sayısı
        r"cihaz adedi",                            # cihaz adedi
        r"kaç cihaz var",                          # kaç cihaz var
        r"kaç adet cihaz var",                     # kaç adet cihaz var
        r"kaç tane cihaz var",                     # kaç tane cihaz var
    ]
    # Eşleşme fonksiyonu
    def match_patterns(patterns):
        for p in patterns:
            if re.search(p, question, re.IGNORECASE):
                return True
        return False
    def is_network_device(h):
        return (
            h.get('interface_type') == '2' or
            any(t.get('tag') == 'network' for t in h.get('tags', []))
        )
    global HOSTS_CACHE, HOSTS_CACHE_TIME
    now = time.time()
    data = request.get_json()
    # Eğer cache'de veri yoksa veya boşsa, anlık veri çek
    if HOSTS_CACHE is not None and len(HOSTS_CACHE) > 0 and (now - HOSTS_CACHE_TIME) < HOSTS_CACHE_TTL:
        hosts = HOSTS_CACHE
    else:
        payload = {
            "jsonrpc": "2.0",
            "method": "host.get",
            "params": {
                "output": ["hostid", "host", "name", "status"],
                "selectGroups": ["name"],
                "selectTags": "extend",
                "selectInventory": ["model", "vendor", "hardware", "hardware_full", "os"]
            },
            "id": 1
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ZABBIX_API_TOKEN}"
        }
        response = requests.post(ZABBIX_URL, json=payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
        data_zabbix = response.json()
        hosts = data_zabbix.get('result', [])
        for host in hosts:
            iface_payload = {
                "jsonrpc": "2.0",
                "method": "hostinterface.get",
                "params": {
                    "output": ["interfaceid", "hostid", "available", "type", "ip"],
                    "hostids": host["hostid"]
                },
                "id": 2
            }
            iface_resp = requests.post(ZABBIX_URL, json=iface_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
            iface_data = iface_resp.json()
            interfaces = iface_data.get('result', [])
            host['available'] = '0'
            host['interface_type'] = None
            host['ip'] = None
            for iface in interfaces:
                if iface.get('type') == '1':
                    host['available'] = iface.get('available', '0')
                    host['interface_type'] = iface.get('type')
                    host['ip'] = iface.get('ip')
                    break
            else:
                if interfaces:
                    host['available'] = interfaces[0].get('available', '0')
                    host['interface_type'] = interfaces[0].get('type')
                    host['ip'] = interfaces[0].get('ip')
            inventory = host.get('inventory') or {}
            host['model'] = inventory.get('model') or inventory.get('hardware') or inventory.get('hardware_full')
            host['vendor'] = inventory.get('vendor')
            host['os'] = inventory.get('os', '')
            host['uptime'] = '-'
            # Uptime bilgisini sırayla farklı anahtarlarla dene
            uptime_keys = [
                "system.uptime",
                "hp.server.hw.uptime[hrSystemUptime]",
                "system.net.uptime[sysUpTime.0]"
            ]
            for key in uptime_keys:
                uptime_payload = {
                    "jsonrpc": "2.0",
                    "method": "item.get",
                    "params": {
                        "output": ["lastvalue"],
                        "hostids": host["hostid"],
                        "search": {"key_": key},
                        "sortfield": "itemid",
                        "sortorder": "ASC",
                        "limit": 1
                    },
                    "id": 3
                }
                uptime_resp = requests.post(ZABBIX_URL, json=uptime_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                uptime_data = uptime_resp.json()
                uptime_items = uptime_data.get('result', [])
                if uptime_items and uptime_items[0].get('lastvalue'):
                    host['uptime'] = uptime_items[0]['lastvalue']
                    break
            # CPU değerini çek
            cpu_keys = [
                "system.cpu.util[,idle]",
                "system.cpu.util[,user]",
                "system.cpu.util"
            ]
            host['cpu'] = '-'
            for key in cpu_keys:
                cpu_payload = {
                    "jsonrpc": "2.0",
                    "method": "item.get",
                    "params": {
                        "output": ["lastvalue"],
                        "hostids": host["hostid"],
                        "search": {"key_": key},
                        "sortfield": "itemid",
                        "sortorder": "ASC",
                        "limit": 1
                    },
                    "id": 4
                }
                cpu_resp = requests.post(ZABBIX_URL, json=cpu_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                cpu_data = cpu_resp.json()
                cpu_items = cpu_data.get('result', [])
                if cpu_items and cpu_items[0].get('lastvalue'):
                    try:
                        val = float(cpu_items[0]['lastvalue'])
                        if key == "system.cpu.util[,idle]":
                            host['cpu'] = f"{100 - val:.1f}"
                        else:
                            host['cpu'] = f"{val:.1f}"
                    except:
                        host['cpu'] = cpu_items[0]['lastvalue']
                    break
            # MEM değerini çek
            mem_keys_percent = [
                "memory.utilization", "mem.utilization", "memory.usage", "mem.usage", "vm.memory.util[vm.memory.util.1]"
            ]
            mem_percent = None
            for key in mem_keys_percent:
                mem_payload = {
                    "jsonrpc": "2.0",
                    "method": "item.get",
                    "params": {
                        "output": ["lastvalue"],
                        "hostids": host["hostid"],
                        "search": {"key_": key},
                        "sortfield": "itemid",
                        "sortorder": "ASC",
                        "limit": 1
                    },
                    "id": 8
                }
                mem_resp = requests.post(ZABBIX_URL, json=mem_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                mem_data = mem_resp.json()
                mem_items = mem_data.get('result', [])
                if mem_items and mem_items[0].get('lastvalue'):
                    try:
                        mem_percent = float(mem_items[0]['lastvalue'])
                        break
                    except:
                        pass
            if mem_percent is None:
                mem_used = None
                mem_total = None
                mem_available = None
                mem_keys_used = [
                    "vm.memory.size[used]", "system.memory.used", "system.mem.used", "memUsedReal", "hrStorageUsed"
                ]
                mem_keys_total = [
                    "vm.memory.size[total]", "system.memory.total", "system.mem.total", "memTotalReal", "hrStorageSize"
                ]
                mem_keys_available = [
                    "vm.memory.size[available]", "vm.memory.size[free]", "system.memory.free", "system.mem.free", "memAvailReal"
                ]
                for key in mem_keys_used:
                    mem_payload = {
                        "jsonrpc": "2.0",
                        "method": "item.get",
                        "params": {
                            "output": ["lastvalue"],
                            "hostids": host["hostid"],
                            "search": {"key_": key},
                            "sortfield": "itemid",
                            "sortorder": "ASC",
                            "limit": 1
                        },
                        "id": 5
                    }
                    mem_resp = requests.post(ZABBIX_URL, json=mem_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                    mem_data = mem_resp.json()
                    mem_items = mem_data.get('result', [])
                    if mem_items and mem_items[0].get('lastvalue'):
                        try:
                            mem_used = float(mem_items[0]['lastvalue'])
                            break
                        except:
                            pass
                for key in mem_keys_total:
                    mem_payload = {
                        "jsonrpc": "2.0",
                        "method": "item.get",
                        "params": {
                            "output": ["lastvalue"],
                            "hostids": host["hostid"],
                            "search": {"key_": key},
                            "sortfield": "itemid",
                            "sortorder": "ASC",
                            "limit": 1
                        },
                        "id": 6
                    }
                    mem_resp = requests.post(ZABBIX_URL, json=mem_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                    mem_data = mem_resp.json()
                    mem_items = mem_data.get('result', [])
                    if mem_items and mem_items[0].get('lastvalue'):
                        try:
                            mem_total = float(mem_items[0]['lastvalue'])
                            break
                        except:
                            pass
                for key in mem_keys_available:
                    mem_payload = {
                        "jsonrpc": "2.0",
                        "method": "item.get",
                        "params": {
                            "output": ["lastvalue"],
                            "hostids": host["hostid"],
                            "search": {"key_": key},
                            "sortfield": "itemid",
                            "sortorder": "ASC",
                            "limit": 1
                        },
                        "id": 7
                    }
                    mem_resp = requests.post(ZABBIX_URL, json=mem_payload, headers=headers, verify=ZABBIX_VERIFY_SSL)
                    mem_data = mem_resp.json()
                    mem_items = mem_data.get('result', [])
                    if mem_items and mem_items[0].get('lastvalue'):
                        try:
                            mem_available = float(mem_items[0]['lastvalue'])
                            break
                        except:
                            pass
                if mem_used is not None and mem_total and mem_total > 0:
                    mem_percent = 100 * mem_used / mem_total
                elif mem_total and mem_available is not None and mem_total > 0:
                    mem_percent = 100 * (mem_total - mem_available) / mem_total
            if mem_percent is not None:
                host['mem'] = f"{mem_percent:.1f}"
            else:
                host['mem'] = '-'
        HOSTS_CACHE = hosts
        HOSTS_CACHE_TIME = time.time()
    question = (data.get('question') or '').lower()
    answer = "Sorunuzu anlayamadım. Lütfen daha açık yazın."

    if not hosts:
        return jsonify({"error": "Host bilgisi bulunamadı"}), 404

    # CPU ve bellek kullanımlarının genel durumunu özetle
    total_cpu = 0
    total_mem = 0
    count_cpu = 0
    count_mem = 0
    for host in hosts:
        if host.get('cpu') != '-':
            try:
                total_cpu += float(host['cpu'])
                count_cpu += 1
            except:
                pass
        if host.get('mem') != '-':
            try:
                total_mem += float(host['mem'])
                count_mem += 1
            except:
                pass
    avg_cpu = total_cpu / count_cpu if count_cpu > 0 else 0
    avg_mem = total_mem / count_mem if count_mem > 0 else 0
    if "cpu ve bellek kullanımları genel olarak nasıl" in question.lower():
        return jsonify({"answer": f"Ortalama CPU: % {avg_cpu:.1f} ve MEMORY: % {avg_mem:.1f} olarak görünmektedir."})

    # CPU kullanımına göre en yüksek 5 host
    if 'cpu kullanımına göre en yüksek' in question:
        cpu_hosts = [h for h in hosts if h.get('cpu') not in (None, '-', '')]
        cpu_hosts_sorted = sorted(cpu_hosts, key=lambda h: float(h['cpu']), reverse=True)[:5]
        if not cpu_hosts_sorted:
            return jsonify({"answer": "CPU verisi bulunamadı."})
        answer = 'CPU kullanımına göre en yüksek değerlere sahip hostlar:\n' + '\n'.join(f"{i+1}- {h.get('name','?')} (%{h.get('cpu')})" for i, h in enumerate(cpu_hosts_sorted))
        return jsonify({"answer": answer})

    # Ağ cihazı
    if match_patterns(network_patterns) and not match_patterns(snmp_patterns):
        network_hosts = [h for h in hosts if is_network_device(h)]
        if match_patterns(list_patterns):
            if len(network_hosts) > 1:
                answer = 'Ağ cihazlarının listesi:\n' + '\n'.join(f"- {h.get('name','?')}" for h in network_hosts)
            elif len(network_hosts) == 1:
                answer = f"Ağ cihazı: {network_hosts[0].get('name','?')}"
            else:
                answer = "Ağ cihazı bulunamadı."
        else:
            answer = f"Ağ cihazı sayısı: {len(network_hosts)}"
        return jsonify({"answer": answer})
    # SNMP
    if match_patterns(snmp_patterns):
        snmp_hosts = [h for h in hosts if str(h.get('interface_type')) == '2' or h.get('interfaceType') == 'SNMP']
        if match_patterns(list_patterns):
            if len(snmp_hosts) > 1:
                answer = 'SNMP ile izlenen hostların listesi:\n' + '\n'.join(f"{i+1}- {h.get('name','?')}" for i, h in enumerate(snmp_hosts))
            elif len(snmp_hosts) == 1:
                answer = f"SNMP ile izlenen host: {snmp_hosts[0].get('name','?')}"
            else:
                answer = "SNMP ile izlenen host bulunamadı."
        else:
            answer = f"SNMP ile izlenen host sayısı: {len(snmp_hosts)}"
        return jsonify({"answer": answer})
    # Agent
    if match_patterns(agent_patterns):
        agent_hosts = [h for h in hosts if str(h.get('interface_type')) == '1' or h.get('interfaceType') == 'Agent']
        if match_patterns(list_patterns):
            if len(agent_hosts) > 1:
                answer = 'Agent ile izlenen hostların listesi:\n' + '\n'.join(f"{i+1}- {h.get('name','?')}" for i, h in enumerate(agent_hosts))
            elif len(agent_hosts) == 1:
                answer = f"Agent ile izlenen host: {agent_hosts[0].get('name','?')}"
            else:
                answer = "Agent ile izlenen host bulunamadı."
        else:
            answer = f"Agent ile izlenen host sayısı: {len(agent_hosts)}"
        return jsonify({"answer": answer})
    # Aktif (erişilebilir/up)
    if match_patterns(aktif_patterns):
        aktif_hosts = [h for h in hosts if h.get('available') == '1']
        if match_patterns(list_patterns):
            if len(aktif_hosts) > 1:
                answer = 'Şu anda erişilebilir (up) hostlar:\n' + '\n'.join(f"- {h.get('name','?')}" for h in aktif_hosts)
            elif len(aktif_hosts) == 1:
                answer = f"Şu anda erişilebilir (up) host: {aktif_hosts[0].get('name','?')}"
            else:
                answer = "Erişilebilir host bulunamadı."
        else:
            answer = f"Erişilebilir host sayısı: {len(aktif_hosts)}"
        return jsonify({"answer": answer})
    # Pasif (erişilemez/down)
    if match_patterns(pasif_patterns):
        pasif_hosts = [h for h in hosts if h.get('available') != '1']
        if match_patterns(list_patterns):
            if len(pasif_hosts) > 1:
                answer = 'Şu anda erişilemez (down) hostlar:\n' + '\n'.join(f"- {h.get('name','?')}" for h in pasif_hosts)
            elif len(pasif_hosts) == 1:
                answer = f"Şu anda erişilemez (down) host: {pasif_hosts[0].get('name','?')}"
            else:
                answer = "Erişilemez host bulunamadı."
        else:
            answer = f"Erişilemez host sayısı: {len(pasif_hosts)}"
        return jsonify({"answer": answer})
    # Tüm hostlar
    if match_patterns(all_patterns):
        if match_patterns(list_patterns):
            if len(hosts) > 1:
                answer = 'Tüm hostların listesi:\n' + '\n'.join(f"{i+1}- {h.get('name','?')}" for i, h in enumerate(hosts))
            elif len(hosts) == 1:
                answer = f"Tek host: {hosts[0].get('name','?')}"
            else:
                answer = "Host bulunamadı."
        else:
            for pattern in host_count_patterns:
                if re.search(pattern, question):
                    answer = f"Toplam {len(hosts)} adet host bulunmaktadır."
                    return jsonify({"answer": answer})
            answer = f"{len(hosts)} Host bulunmaktadır."
        return jsonify({"answer": answer})

    return jsonify({"answer": answer})

if __name__ == '__main__':
    t = threading.Thread(target=poll_hosts_background, daemon=True)
    t.start()
    app.run(debug=True, host='0.0.0.0', port=5000) 