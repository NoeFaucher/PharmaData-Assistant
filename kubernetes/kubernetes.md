# Déploiement Kubernetes — PharmaData API

Ce guide décrit les étapes pour construire l'image Docker, la publier dans un registry, et déployer l'application sur un cluster Kubernetes.

---

## Différences par rapport à l'architecture décrite

Ce déploiement diffère sur plusieurs points de l'architecture de référence :

| Point | Architecture décrite | Ce déploiement |
|---|---|---|
| **Reverse proxy** | Nginx Ingress Controller | **Pod Nginx dédié** (Deployment + Service LoadBalancer) |
| **Client web** | Frontend déployé sur le cluster | **Non déployé** — `app.py` Streamlit tourne localement et consomme l'API distante |
| **Registry d'images** | Registry d'entreprise | **Registry personnel** (Docker Hub dans les exemples ci-dessous, à adapter) |

---

## Prérequis

- `docker` installé et daemon en cours d'exécution
- `kubectl` configuré avec accès au cluster (configurer son `kubeconfig.yaml`)
- Compte sur un registry de conteneurs (Docker Hub ou registry personnel)
- OVH Secret manager configuré avec les secrets de l'application 

---

## 1. Construire l'image Docker

Le `Dockerfile` se trouve dans `docker/Dockerfile`. Le contexte de build est la racine du projet.

```bash
# Depuis la racine du projet
docker build -f docker/Dockerfile -t <votre-registry>/<nom-image>:<tag> .

# Exemple avec Docker Hub (remplacer "monuser" par votre identifiant)
docker build -f docker/Dockerfile -t monuser/pharma-api:latest .
```

> Le `docker-compose.yml` utilise `context: ..` et `dockerfile: ./docker/Dockerfile`, ce qui correspond exactement à la commande ci-dessus.

Pour tagger plusieurs versions :
```bash
docker build -f docker/Dockerfile \
  -t monuser/pharma-api:latest \
  -t monuser/pharma-api:1.0.0 \
  .
```

---

## 2. Publier l'image dans le registry

```bash
# Se connecter au registry
docker login
# (Pour un registry privé : docker login registry.example.com)

# Pousser l'image
docker push monuser/pharma-api:latest
docker push monuser/pharma-api:1.0.0   # si vous avez tagué une version spécifique
```

Après publication, mettre à jour le champ `image:` dans `kubernetes_conf.yaml` :

```yaml
# kubernetes/kubernetes_conf.yaml
containers:
  - name: pharma-api
    image: monuser/pharma-api:latest   # ← adapter ici
```

Si votre registry est privé, vous devrez également créer un `imagePullSecret` :
```bash
kubectl create secret docker-registry regcred \
  --docker-server=<registry> \
  --docker-username=<user> \
  --docker-password=<token> \
  -n pharma
```
Et référencer ce secret dans le Deployment (`spec.template.spec.imagePullSecrets`).

---

## 3. Configurer les secrets (OVH Secret manager + ESO)

Les secrets applicatifs (`DATABASE_URL`, `DATABASE_URL_SYNC`, `OPENROUTER_API_KEY`, `JWT_SECRET_KEY`) sont stockés dans OVH Secret manager et injectés automatiquement dans le cluster via l'External Secrets Operator.

Installation de l'External Secret Operator (ESO) sur votre cluster Kubernetes:
```bash
helm repo add external-secrets https://charts.external-secrets.io
helm repo update

helm install external-secrets \
   external-secrets/external-secrets \
    -n external-secrets \
    --create-namespace \
    --set installCRDs=true
```

Pour configurer les secrets dans OVH Secret manager, suivez la [documentation OVH KMS](https://help.ovhcloud.com/csm/fr-key-management-service?id=kb_article_view&sysparm_article=KB0063362).

Une fois les secrets créés dans le Secret manager, appliquer dans l'ordre :

```bash
# 1. Créer le namespace
kubectl apply -f kubernetes/namespace.yaml

# 2. Créer le bootstrap token pour accéder au Secret manager OVH
#    (contient le token d'authentification encodé en base64)
kubectl apply -f kubernetes/external-secret/secret_ovhcloud_token.yaml -n external-secrets

# 3. Déployer le ClusterSecretStore et l'ExternalSecret
kubectl apply -f kubernetes/external-secret/external-secret.yaml

# 4. Les Secret pour l'api
kubectl apply -f kubernetes/secret_pharma.yaml
```

Vérifier que les secrets sont bien synchronisés :
```bash
kubectl get externalsecret -n pharma
# READY doit afficher "True"

kubectl get secret pharma-secrets -n pharma
```

---

## 4. Déployer l'application

```bash
kubectl apply -f kubernetes/nginx.yaml
kubectl apply -f kubernetes/api.yaml
```

Ce manifest déploie :
- **Deployment `pharma-api`** — 2 replicas de l'API FastAPI
- **Service `pharma-api-svc`** — ClusterIP interne vers l'API
- **Deployment `nginx`** — pod Nginx reverse proxy (SSE, timeouts longs)
- **Service `nginx-svc`** — LoadBalancer exposant le port 80 vers l'extérieur
- **ConfigMap `pharma-config`** — variables non-sensibles (algorithme JWT, durées de tokens)
- **ConfigMap `nginx-config`** — configuration Nginx avec support SSE

---

## 5. Vérifier le déploiement

```bash
# État des pods
kubectl get pods -n pharma

# État des services
kubectl get svc -n pharma

# Logs de l'API
kubectl logs -l app=pharma-api -n pharma --tail=50

# Logs Nginx
kubectl logs -l app=nginx -n pharma --tail=20
```

---

## 6. Accéder à l'API

L'accès externe passe par le Service LoadBalancer Nginx :

```bash
kubectl get svc nginx-svc -n pharma
# Récupérer la valeur dans la colonne EXTERNAL-IP
```

| Endpoint | URL |
|---|---|
| Santé | `http://<EXTERNAL-IP>/health` |
| Swagger UI | `http://<EXTERNAL-IP>/docs` |
| API | `http://<EXTERNAL-IP>/` |

L'application Streamlit (`app.py`) peut alors pointer vers cette IP en configurant l'URL de l'API dans son interface.

---

## 7. Mise à jour de l'image

Pour déployer une nouvelle version :

```bash
# 1. Reconstruire et pousser l'image
docker build -f docker/Dockerfile -t monuser/pharma-api:1.1.0 .
docker push monuser/pharma-api:1.1.0

# 2. Mettre à jour le tag dans kubernetes_conf.yaml, puis appliquer
kubectl apply -f kubernetes/kubernetes_conf.yaml

# Ou forcer un rollout sans changer le manifest (si vous utilisez :latest)
kubectl rollout restart deployment/pharma-api -n pharma
```

Suivre le rollout :
```bash
kubectl rollout status deployment/pharma-api -n pharma
```
