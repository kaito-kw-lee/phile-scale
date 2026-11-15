# Raspberry Pi 광고차단 CLI

Pi-hole + Unbound + Tailscale을 Docker Compose로 운영합니다. 대상은 **Debian 13 / Debian 13 기반 Raspberry Pi OS 이상, ARM64, systemd, NetworkManager**입니다. 32비트 OS는 지원하지 않습니다. 신규 OS는 사전 기능 검사를 통과해야 하며, 아직 출시되지 않은 버전의 호환성을 보장하지 않습니다.

Raspberry Pi 4 ARM64 실장비에서 DNS/DNSSEC, 차단·허용·삭제, 백업 복원, 비허용 인터페이스의 TCP/UDP 53·5335 차단, 120초 자동 네트워크 복구, 재부팅 후 자동 시작을 검증했습니다. Tailscale 인증과 컨테이너 재시작 후 인증 유지도 확인했습니다. 별도 LAN PC에서도 TCP/UDP DNS 조회를 확인했습니다. 자동 테스트는 14개이며 실검증 스크립트는 `tests/live_verify.py`입니다. 검증에 사용한 공식 이미지 digest는 `release-images.env`에 고정되어 있습니다.

## 설치

저장소를 Pi에 복사한 다음 실행합니다. Pi-hole v6 및 Tailscale의 검토한 공식 이미지 버전 태그 또는 digest를 지정합니다. `latest`를 기본값으로 사용하지 않으며 설치 시 태그를 immutable digest로 고정합니다. Unbound는 digest로 고정한 Debian 기반 이미지에서 빌드하고 로컬 image ID로 고정합니다.

```sh
# 실장비에서 검증한 digest 사용 (Pi-hole 2026.11.0 / Tailscale v1.104.7)
. ./release-images.env
sudo sh install.sh --pihole-image "$PIHOLE_IMAGE" --tailscale-image "$TAILSCALE_IMAGE" --base-image "$BASE_IMAGE"
sudo systemctl status adblock
sudo adblock tailscale login
sudo adblock doctor
```

필요한 Docker/Compose는 Debian 저장소의 `docker.io`, `docker-compose` 패키지로 설치합니다. 기존 Docker가 있으면 유지합니다. NTP 시간 동기화, 2GiB 이상의 여유 공간, `/dev/net/tun`, 53번 포트 사용 가능 여부를 먼저 검사합니다. 기존 DNS 서비스는 임의 종료하지 않습니다. 이미지 다운로드와 Unbound 재귀 조회를 위해 인터넷 연결 및 외부 TCP/UDP 53 접근이 필요합니다.

설치 완료 메시지는 systemd 시작 요청이 등록되었다는 뜻입니다. `systemctl status adblock`과 `adblock doctor`가 최종 실행 결과입니다. Tailscale 로그인 전에는 인증 진단이 실패하는 것이 정상입니다.

초기 LAN IP는 현재 연결에서 감지합니다. 공유기에서 해당 IP를 DHCP 예약하고, LAN DHCP DNS 서버를 Pi의 IP로 지정합니다. 공유기가 광고하는 IPv6 DNS 및 브라우저/앱 자체 DoH는 Pi-hole을 우회할 수 있으므로 별도로 조정합니다. 이 패키지는 IPv6 DNS 수신을 차단하며 DHCP 서버로 동작하지 않습니다.

## 외부 접속

`adblock tailscale login`이 표시하는 URL로 로그인합니다. 표시된 Tailscale IPv4를 Tailnet의 global nameserver로 등록하고 DNS override를 활성화합니다. 클라이언트에서는 Tailscale DNS를 수용하고, Tailnet grants/ACL에서 허용 기기의 TCP/UDP 53 및 관리자 SSH 22 접근을 허용합니다. Pi 자체는 `--accept-dns=false`로 설정합니다.

```sh
sudo adblock tailscale status
ssh lkw@<Pi의-Tailscale-IP>
```

Exit Node나 서브넷 라우팅은 설정하지 않습니다. 기존 SSH 서버를 사용하며 SSH 설치·인증 정책은 변경하지 않습니다. 기존 호스트 방화벽이 DNS를 막으면 별도 허용 규칙이 필요합니다. 패키지 방화벽은 다른 방화벽의 DROP을 무효화하지 않습니다.

## IP 변경

```sh
sudo adblock network show
sudo adblock network save home
sudo adblock network set --interface eth0 --address 192.168.10.2/24 \
  --gateway 192.168.10.1 --allow-cidr 192.168.10.0/24
# 새 IP 또는 Tailscale로 재접속한 후, 120초 이내
sudo adblock network confirm
# 저장된 구성 재사용
sudo adblock network set --profile home
```

실제 인터페이스는 `network show`에서 확인합니다. 허용 대역은 `--allow-cidr`를 반복해 지정합니다. 호스트 부팅용 DNS는 현재 LAN DNS를 보존하며 필요하면 `--host-dns <외부-DNS-IP>`를 반복 지정합니다. 다른 서브넷으로 이동할 때에는 새 네트워크에서도 도달 가능한 호스트 DNS를 지정해야 합니다. 공유기/케이블 연결도 해당 서브넷에 맞아야 합니다. IP 중복 탐지는 같은 링크에서 응답하는 장치만 탐지할 수 있습니다.

원래 NetworkManager 연결 프로필의 IPv4 설정을 저널에 기록하고 복제본을 추가 보관한 뒤 systemd 작업에서 변경합니다. 복구는 원래 UUID의 설정을 되돌리므로 DHCP 연결 식별자를 유지합니다. NetworkManager의 대화형 checkpoint 대신 영속 저널과 독립적인 5초 주기 복구 타이머를 사용하여 SSH 종료와 재부팅에도 복구를 재시도합니다. 미확정 변경은 120초 이후 다음 타이머 주기에 복구합니다. DHCP가 다른 주소를 배정하면 실제 주소를 설정에 반영합니다. 변경 중에는 백업·업데이트 등 긴 작업을 차단합니다. 인터페이스 자체 변경은 지원하지 않습니다.

Tailscale 컨테이너는 `tailscaled`를 직접 실행합니다. 인증 전에도 데몬이 계속 동작하므로 로그인 대기 시간 초과로 컨테이너가 재시작하지 않습니다. 로그인은 CLI로 명시적으로 수행합니다.

## 운영 명령

```sh
sudo adblock status --json
sudo adblock doctor --json
sudo adblock logs pihole
sudo adblock block add ads.example.com
sudo adblock block remove ads.example.com
sudo adblock allow add trusted.example.com
sudo adblock allow remove trusted.example.com
sudo adblock backup
sudo adblock restore /var/lib/adblock/backups/<backup>.tar.gz
sudo adblock update --pihole-image "$PIHOLE_IMAGE" --tailscale-image "$TAILSCALE_IMAGE"
sudo adblock rollback
sudo adblock stop
sudo adblock start
sudo adblock uninstall
# 데이터까지 삭제할 때만: sudo adblock uninstall --purge-data
```

설정 `/etc/adblock`, 데이터 `/var/lib/adblock`, 실행 자산 `/opt/adblock/current`를 사용합니다. Docker 소켓과 비밀 설정을 읽어야 하므로 조회 명령도 sudo로 실행합니다. 웹 UI는 `127.0.0.1:8080`에만 제공하며 무작위 암호는 `/etc/adblock/webpassword`에 저장합니다. 기본 운영에는 웹 UI가 필요하지 않습니다.

Pi-hole 쿼리 기록은 7일, 컨테이너 로그는 서비스당 10MB × 3개입니다. 백업은 서비스 정지 중 생성하여 DB 일관성을 확보하므로 짧은 DNS 중단이 발생합니다. 최근 5개 외에 롤백 대상과 현재 복원 대상으로 보호된 백업이 추가로 남을 수 있습니다. 백업에는 Tailscale 인증 정보가 있으므로 외부 복사본도 비공개로 보관합니다.

백업·복원·업데이트는 Tailscale 컨테이너도 중단합니다. 이 명령들은 LAN SSH/로컬 콘솔에서 실행하거나, 원격에서는 `sudo systemd-run --unit=adblock-maintenance --collect /usr/local/bin/adblock backup`처럼 독립 systemd 작업으로 실행합니다. 재접속 후 `journalctl -u adblock-maintenance`에서 결과를 확인합니다.

업데이트 실패 시 이미지 핀과 데이터 백업을 함께 복원합니다. 복원은 현재 LAN 설정을 유지합니다. 호스트 밖으로 백업을 이동할 경우 Unbound 로컬 이미지도 `docker image save`로 별도 보관해야 합니다. 자동 이미지 정리/prune은 이전 Unbound 이미지를 삭제할 수 있으므로 롤백 보존 기간에는 수행하지 않습니다. 기반 이미지 digest를 고정해도 apt 패키지는 빌드 시점 저장소를 사용하므로 재빌드 자체가 비트 단위 재현 가능한 것은 아닙니다.

제거는 서비스와 패키지 소유 방화벽 규칙만 제거합니다. Docker·다른 컨테이너·호스트 LAN 설정·소스 파일은 유지합니다. 데이터 보존 제거 후 `install`을 다시 실행하면 systemd 유닛을 재등록합니다.

## 검증

개발 환경에서:

```sh
python -m unittest discover -s tests -v
python -m py_compile adblock.py
```

실제 Pi 수용 테스트:

1. 설치 후 `doctor`의 DNS, DNSSEC, 방화벽, 시간, 디스크, Tailscale 항목을 확인합니다.
2. LAN 및 외부 Tailscale 기기에서 `dig @<Pi-IP> example.com`과 `dig +tcp @<Pi-IP> example.com`을 실행합니다.
3. `example.com`을 일시 차단하고 두 경로에서 차단 응답을 확인한 다음 제거합니다.
4. 허용되지 않은 LAN 기기에서 DNS 접근 실패, 외부에서 5335 접근 실패를 확인합니다. IPv6 53도 실패해야 합니다.
5. IP 변경 후 confirm 성공, 미확정 시간 초과, SSH 연결 종료, 변경 중 재부팅을 각각 테스트합니다.
6. 재부팅 후 방화벽이 Docker보다 먼저 적용되고 DNS·Tailscale 인증이 복구되는지 확인합니다.
7. 백업 후 차단 목록 변경 → 복원으로 원래 상태가 되는지 확인합니다. 업데이트 실패/rollback도 테스트합니다.
8. Unbound 중단, 포트 충돌, 디스크 부족, Tailscale 인증 만료가 진단되는지 확인합니다.

공식 참고: [Pi-hole Docker](https://docs.pi-hole.net/docker/), [Pi-hole CLI](https://docs.pi-hole.net/main/pihole-command/), [Unbound](https://docs.pi-hole.net/guides/dns/unbound/), [Tailscale Docker](https://tailscale.com/docs/features/containers/docker), [Debian Compose](https://packages.debian.org/trixie/docker-compose).
