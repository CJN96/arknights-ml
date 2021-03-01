import time
from io import BytesIO

import numpy as np
import cv2
import os
import subprocess
from PIL import Image

import inventory
import torch
import torch.nn as nn
from dl_data import download_icons

import json


collect_path = 'images/collect/'


# 更新素材
print('更新素材')
download_icons()


def dump_index_itemid_relation():
    dump_data = {
        'idx2id': [],
        'id2idx': {}
    }
    index = 0
    for dirpath in os.listdir(collect_path):
        item_id = dirpath
        dump_data['idx2id'].append(item_id)
        dump_data['id2idx'][item_id] = index
        index += 1
    with open('index_itemid_relation.json', 'w') as f:
        json.dump(dump_data, f)
    return dump_data['idx2id'], dump_data['id2idx']


def load_images():
    img_map = {}
    gray_img_map = {}
    img_files = []
    for (dirpath, dirnames, filenames) in os.walk('images/collect'):
        if filenames:
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                with open(filepath, 'rb') as f:
                    nparr = np.frombuffer(f.read(), np.uint8)
                    # convert to image array
                    image = cv2.imdecode(nparr, cv2.IMREAD_UNCHANGED)
                    image = cv2.resize(image, (128, 128))
                    if image.shape[-1] == 4:
                        image = image[..., :-1]
                    gray_img = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                    img_map[filepath] = image
                    gray_img_map[filepath] = gray_img
                    img_files.append(filepath)
    return img_map, gray_img_map, img_files


idx2id, id2idx = dump_index_itemid_relation()
img_map, gray_img_map, img_files = load_images()
NUM_CLASS = len(idx2id)
print('NUM_CLASS', NUM_CLASS)


def crop_item_middle_img(cv_item_img, ox, oy, radius):
    ratio = radius / 60
    y1 = int(oy - (40 * ratio))
    y2 = int(oy + (24 * ratio))
    x1 = int(ox - (32 * ratio))
    x2 = int(ox + (32 * ratio))
    return cv2.resize(cv_item_img[y1:y2, x1:x2], (64, 64))


def get_noise_data():
    images_np = np.random.rand(40, 64, 64, 3)
    labels_np = np.asarray(['other']).repeat(40)
    return images_np, labels_np


def get_data():
    images = []
    labels = []
    for filepath in img_files:
        item_id = filepath.split('\\')[-2]

        image = img_map[filepath]
        # print(filepath)
        # inventory.show_img(image)
        item_gray = gray_img_map[filepath]
        circles = inventory.get_circles(item_gray, 50, 100)
        c = circles[0]
        t = 10 if item_id.isdigit() else 1
        for _ in range(t):
            ox = c[0] + np.random.randint(-3, 3)
            oy = c[1] + np.random.randint(-3, 3)
            img = crop_item_middle_img(image, ox, oy, c[2])

            image_aug = img

            images.append(image_aug)
            labels.append(id2idx[item_id])
    images_np = np.transpose(np.stack(images, 0), [0, 3, 1, 2])
    labels_np = np.array(labels)

    rand = np.random.RandomState(321)
    shuffle = rand.permutation(len(images_np))
    images_np = images_np[shuffle]
    labels_np = labels_np[shuffle]

    # print(images_np.shape)
    return images_np, labels_np


class Cnn(nn.Module):
    def __init__(self):  # 1x28x28
        super(Cnn, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(3, 6, 3, stride=1, padding=1),  # 6 * 64 * 64
            nn.ReLU(True),
            nn.MaxPool2d(2, 2),  # 6 * 32 * 32
            nn.Conv2d(6, 16, 5, stride=1, padding=0),  # 16 * 28 *28
            nn.ReLU(True),
            nn.MaxPool2d(4, 4))  # # 16 * 7 * 7

        self.fc = nn.Sequential(
            nn.Linear(784, 400),  # 784 = 16 * 7 * 7
            nn.ReLU(True),
            nn.Linear(400, 120),
            nn.ReLU(True),
            nn.Linear(120, NUM_CLASS))

    def forward(self, x):
        out = self.conv(x)
        out = out.reshape(-1, 16 * 7 * 7)  # 784 = 16 * 7 * 7
        out = self.fc(out)
        return out


def compute_loss(x, label):
    loss = nn.CrossEntropyLoss()(x, label)
    prec = (x.argmax(1) == label).float().mean()
    return loss, prec


def train():
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    print('train on:', device)
    model = Cnn().to(device)
    optim = torch.optim.Adam(model.parameters(), lr=1e-3)
    model.train()
    for step in range(300):
        images_aug_np, label_np = get_data()
        images_aug = torch.from_numpy(images_aug_np).float().to(device)
        label = torch.from_numpy(label_np).long().to(device)
        optim.zero_grad()
        score = model(images_aug)
        loss, prec = compute_loss(score, label)
        loss.backward()
        optim.step()
        if step < 10 or step % 10 == 0:
            print(step, loss.item(), prec.item())
    torch.save(model.state_dict(), './model.bin')
    torch.onnx.export(model, images_aug, 'ark_material.onnx')


def load_model():
    model = Cnn()
    device = torch.device('cpu')
    model.load_state_dict(torch.load('./model.bin', map_location=device))
    model.eval()
    return model


def predict(model, roi_list):
    """
    Image size of 720p is recommended.
    """
    roi_np = np.stack(roi_list, 0)
    roi_t = torch.from_numpy(roi_np).float()
    with torch.no_grad():
        score = model(roi_t)
        probs = nn.Softmax(1)(score)
        predicts = score.argmax(1)

    probs = probs.cpu().data.numpy()
    predicts = predicts.cpu().data.numpy()
    return [idx2id[p] for p in predicts], [probs[i, predicts[i]] for i in range(len(roi_list))]


def test():
    model = load_model()
    screen = Image.open('images/screen.png')
    # screen = screenshot()
    items = inventory.get_all_item_img_in_screen(screen)
    roi_list = []
    for x in items:
        roi = x['rectangle2'].copy()
        roi = np.transpose(roi, (2, 0, 1))
        roi_list.append(roi)
    res = predict(model, roi_list)
    print(res)
    for i in range(len(res[0])):
        item_id = res[0][i]
        if item_id == 'other':
            print(res[1][i], 'other')
        else:
            print(res[1][i], inventory.item_map[item_id])
        inventory.show_img(items[i]['rectangle'])


def screenshot():
    content = subprocess.check_output('adb exec-out "screencap -p"', shell=True)
    if os.name == 'nt':
        content = content.replace(b'\r\n', b'\n')
    # with open('images/screen.png', 'wb') as f:
    #     f.write(content)
    # img_array = np.asarray(bytearray(content), dtype=np.uint8)
    return Image.open(BytesIO(content))


def save_collect_img(item_id, img):
    if not os.path.exists(collect_path + item_id):
        os.mkdir(collect_path + item_id)
    cv2.imwrite(collect_path + item_id + '/%s.png' % int(time.time() * 1000), img)


def prepare_train_resource():
    model = load_model()
    screen = screenshot()
    items = inventory.get_all_item_img_in_screen(screen)
    roi_list = []
    for x in items:
        roi = x['rectangle2'].copy()
        # inventory.show_img(roi)
        roi = np.transpose(roi, (2, 0, 1))
        roi_list.append(roi)
    res = predict(model, roi_list)
    print(res)
    for i in range(len(res[0])):
        item_id = res[0][i]
        print(res[1][i], inventory.item_map[int(item_id)])
        if res[1][i] < 0.1:
            item_id = 'other'
        else:
            keycode = inventory.show_img(items[i]['rectangle2'])
            if keycode != 13:
                item_id = 'other'
        print(item_id)
        save_collect_img(item_id, items[i]['rectangle'])


def test_cv_onnx():
    net = cv2.dnn.readNetFromONNX('ark_material.onnx')
    screen = Image.open('images/screen.png')
    # screen = screenshot()
    items = inventory.get_all_item_img_in_screen(screen)
    roi_list = []
    for x in items:
        roi = x['rectangle2'].copy()
        # inventory.show_img(roi)

        roi_list.append(roi)
        blob = cv2.dnn.blobFromImage(roi)
        net.setInput(blob)
        out = net.forward()

        # Get a class with a highest score.
        out = out.flatten()
        classId = np.argmax(out)
        confidence = out[classId]
        item_id = idx2id[classId]
        print(confidence, inventory.item_map[item_id] if item_id.isdigit() else item_id)
        inventory.show_img(roi)


def export_onnx():
    model = load_model()
    screen = Image.open('images/screen.png')
    items = inventory.get_all_item_img_in_screen(screen)
    roi_list = []
    for x in items:
        roi = x['rectangle2'].copy()
        roi = np.transpose(roi, (2, 0, 1))
        roi_list.append(roi)
    roi_np = np.stack(roi_list, 0)
    roi_t = torch.from_numpy(roi_np).float()
    torch.onnx.export(model, roi_t, 'ark_material.onnx')


if __name__ == '__main__':
    train()
    # test()
    # prepare_train_resource()
    # export_onnx()
    # test_cv_onnx()


