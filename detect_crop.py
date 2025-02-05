import argparse
import time
import numpy as np
from pathlib import Path

import cv2
import torch
import torch.backends.cudnn as cudnn
from numpy import random

from models.experimental import attempt_load
from utils.datasets import LoadStreams, LoadImages
from utils.general import check_img_size, check_requirements, check_imshow, non_max_suppression, apply_classifier, \
    scale_coords, xyxy2xywh, strip_optimizer, set_logging, increment_path
from utils.plots import plot_one_box
from utils.torch_utils import select_device, load_classifier, time_synchronized, TracedModel

def sort_vertices_clockwise_or_anticlockwise(vertices):
    # Check if there are any vertices
    if len(vertices) == 0:
        return vertices

    # Calculate the center of the vertices (assuming the points are in (x, y) format)
    if len(vertices) >= 2:
        cx = np.mean(vertices[:, 0])
        cy = np.mean(vertices[:, 1])
    else:
        # If there are not enough points, return the original vertices without sorting
        return vertices

    # Calculate the angle between each vertex and the center point
    angles = np.arctan2(vertices[:, 1] - cy, vertices[:, 0] - cx)

    # Sort the vertices based on the angles
    sorted_indices = np.argsort(angles)
    sorted_vertices = vertices[sorted_indices]

    return sorted_vertices


def get_perspective_transformed_image(image, vertices=None):
    """
    Obtener la transformación de perspectiva para recortar el documento.
    Si los bordes principales no se detectan, utiliza el bounding box original con offset.
    """
    width = 200  # Ancho deseado del documento recortado
    height = 300  # Alto deseado del documento recortado

    if vertices is not None:
        # Puntos de destino para la transformación de perspectiva
        dst_points = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)

        # Calcular la matriz de transformación
        M = cv2.getPerspectiveTransform(vertices.astype(np.float32), dst_points)

        # Realizar la transformación de perspectiva
        warped_image = cv2.warpPerspective(image, M, (width, height))
    else:
        # Si no se detectaron bordes, utilizar el bounding box original con offset
        offset = opt.offset  # Puedes ajustar el valor del offset según tus necesidades
        x_min, y_min, x_max, y_max = offset, offset, image.shape[1] - offset, image.shape[0] - offset

        # Puntos de destino para la transformación de perspectiva
        dst_points = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)

        # Puntos de origen para la transformación de perspectiva utilizando el bounding box original con offset
        src_points = np.array([[x_min, y_min], [x_max, y_min], [x_max, y_max], [x_min, y_max]], dtype=np.float32)

        # Calcular la matriz de transformación
        M = cv2.getPerspectiveTransform(src_points, dst_points)

        # Realizar la transformación de perspectiva
        warped_image = cv2.warpPerspective(image, M, (width, height))

    return warped_image


def detect(save_img=False):
    source, weights, view_img, save_txt, imgsz, trace = opt.source, opt.weights, opt.view_img, opt.save_txt, opt.img_size, not opt.no_trace
    save_img = not opt.nosave and not source.endswith('.txt')  # save inference images
    webcam = source.isnumeric() or source.endswith('.txt') or source.lower().startswith(
        ('rtsp://', 'rtmp://', 'http://', 'https://'))

    # Directories
    save_dir = Path(increment_path(Path(opt.project) / opt.name, exist_ok=opt.exist_ok))  # increment run
    (save_dir / 'labels' if save_txt else save_dir).mkdir(parents=True, exist_ok=True)  # make dir

    cropped_dir = save_dir / 'cropped' # crop images dir
    cropped_dir.mkdir(parents=True, exist_ok=True) 

    # Initialize
    set_logging()
    device = select_device(opt.device)
    half = device.type != 'cpu'  # half precision only supported on CUDA

    # Load model
    model = attempt_load(weights, map_location=device)  # load FP32 model
    stride = int(model.stride.max())  # model stride
    imgsz = check_img_size(imgsz, s=stride)  # check img_size

    if trace:
        model = TracedModel(model, device, opt.img_size)

    if half:
        model.half()  # to FP16

    # Second-stage classifier
    classify = False
    if classify:
        modelc = load_classifier(name='resnet101', n=2)  # initialize
        modelc.load_state_dict(torch.load('weights/resnet101.pt', map_location=device)['model']).to(device).eval()

    # Set Dataloader
    vid_path, vid_writer = None, None
    if webcam:
        view_img = check_imshow()
        cudnn.benchmark = True  # set True to speed up constant image size inference
        dataset = LoadStreams(source, img_size=imgsz, stride=stride)
    else:
        dataset = LoadImages(source, img_size=imgsz, stride=stride)

    # Get names and colors
    names = model.module.names if hasattr(model, 'module') else model.names
    colors = [[random.randint(0, 255) for _ in range(3)] for _ in names]

    # Run inference
    if device.type != 'cpu':
        model(torch.zeros(1, 3, imgsz, imgsz).to(device).type_as(next(model.parameters())))  # run once
    old_img_w = old_img_h = imgsz
    old_img_b = 1

    t0 = time.time()
    for path, img, im0s, vid_cap in dataset:
        img = torch.from_numpy(img).to(device)
        img = img.half() if half else img.float()  # uint8 to fp16/32
        img /= 255.0  # 0 - 255 to 0.0 - 1.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)

        # Warmup
        if device.type != 'cpu' and (old_img_b != img.shape[0] or old_img_h != img.shape[2] or old_img_w != img.shape[3]):
            old_img_b = img.shape[0]
            old_img_h = img.shape[2]
            old_img_w = img.shape[3]
            for i in range(3):
                model(img, augment=opt.augment)[0]

        # Inference
        t1 = time_synchronized()
        with torch.no_grad():   # Calculating gradients would cause a GPU memory leak
            pred = model(img, augment=opt.augment)[0]
        t2 = time_synchronized()

        # Apply NMS
        pred = non_max_suppression(pred, opt.conf_thres, opt.iou_thres, classes=opt.classes, agnostic=opt.agnostic_nms)
        t3 = time_synchronized()

        # Apply Classifier
        if classify:
            pred = apply_classifier(pred, modelc, img, im0s)

        # Process detections
        for i, det in enumerate(pred):  # detections per image
            if webcam:  # batch_size >= 1
                p, s, im0, frame = path[i], '%g: ' % i, im0s[i].copy(), dataset.count
            else:
                p, s, im0, frame = path, '', im0s, getattr(dataset, 'frame', 0)

            p = Path(p)  # to Path
            save_path = str(save_dir / p.name)  # img.jpg
            txt_path = str(save_dir / 'labels' / p.stem) + ('' if dataset.mode == 'image' else f'_{frame}')  # img.txt
            gn = torch.tensor(im0.shape)[[1, 0, 1, 0]]  # normalization gain whwh
            if len(det):
                # Rescale boxes from img_size to im0 size
                det[:, :4] = scale_coords(img.shape[2:], det[:, :4], im0.shape).round()

                # Process each detection
                for *xyxy, conf, cls in reversed(det):
                    x_min, y_min, x_max, y_max = map(int, xyxy)

                    # Aplicar offset hacia afuera del recorte del ROI
                    offset = opt.offset  # value can be modified default 20
                    x_min = max(0, x_min - offset)
                    y_min = max(0, y_min - offset)
                    x_max = min(im0.shape[1], x_max + offset)
                    y_max = min(im0.shape[0], y_max + offset)

                    # Recorte del bounding box
                    roi = im0[y_min:y_max, x_min:x_max]

                    if opt.edge_enhancer == 0: #Laplacian edge enhancer

                    # Convertir la imagen de laplacian a un rango de 0 a 255
                        enhancer = cv2.convertScaleAbs(roi)
                    
                    # Convertir la imagen filtrada (mejora de bordes) a escala de grises
                        enhancer_gray = cv2.cvtColor(enhancer, cv2.COLOR_BGR2GRAY)

                    elif opt.edge_enhancer == 1: #Sobel edge enhancer
                    
                    # Aplicar filtro de bordes de Sobel en el canal verde de la imagen
                        sobel_x = cv2.Sobel(roi[:, :, 1], cv2.CV_64F, 1, 0, ksize=3)
                        sobel_y = cv2.Sobel(roi[:, :, 1], cv2.CV_64F, 0, 1, ksize=3)
                        sobel = np.sqrt(sobel_x**2 + sobel_y**2)
                        enhancer = np.uint8(sobel)

                        enhancer_gray = enhancer
                    else: #opt.edge_enhancer == 2 or others # Canny edge enhancer
                    
                    # Aplicar filtro de bordes de Canny en el canal verde de la imagen
                        enhancer = cv2.Canny(roi[:, :, 1], threshold1=50, threshold2=150)

                    # Convertir la imagen con el filtro de bordes de Sobel a escala de grises
                        #enhancer_gray = cv2.cvtColor(enhancer, cv2.COLOR_BGR2GRAY)
                        enhancer_gray = enhancer

                    # Recorte de la sección restante después de aplicar el filtro de mejora de bordes
                    new_roi = enhancer[y_min:y_max, x_min:x_max]

                    # Guardar imágenes recortadas con filtro enhancero
                    save_enhancer_path = str(cropped_dir / (p.stem + f'_{i}_enhancer.jpg'))
                    if enhancer is not None and not np.all(enhancer == 0):
                        cv2.imwrite(save_enhancer_path, enhancer)
                    else:
                        print("no edge detected using edge enhancer, image not saved")

                    # # Guardar imágenes recortadas después de aplicar filtro enhancero
                    # save_new_roi_path = str(cropped_dir / (p.stem + f'_{i}__cropped_new_roi.jpg'))
                    # #cv2.imwrite(save_new_roi_path, new_roi)
                    # if new_roi is not None and not np.all(new_roi == 0):
                    #     cv2.imwrite(save_new_roi_path, new_roi)
                    # else:
                    #     print("no crop roi, image not saved")

                    # # Considerar esta aplicación para futuros desarrollos para recorte de bordes de documento medinate extracción de bordes externos con computer vision tradicional    

                    # # Aplicar un umbral binario para convertir los bordes resaltados en una imagen binaria
                    # _, binary_image = cv2.threshold(enhancer_gray, 50, 255, cv2.THRESH_BINARY)

                    # # Detectar los contornos en la imagen binaria
                    # contours, _ = cv2.findContours(binary_image, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

                    # # Identificar el contorno más grande, que debe corresponder al rectángulo del documento de identificación
                    # largest_contour = max(contours, key=cv2.contourArea)

                    # # Aproximar el contorno del rectángulo para obtener sus vértices
                    # epsilon = 0.1 * cv2.arcLength(largest_contour, True)
                    # approx_vertices = cv2.approxPolyDP(largest_contour, epsilon, True)

                    # # Si el documento es un rectángulo, approx_vertices debe contener cuatro vértices
                    # if len(approx_vertices) == 4:
                    #     # Ordenar los vértices en sentido horario o antihorario
                    #     sorted_vertices = sort_vertices_clockwise_or_anticlockwise(approx_vertices)

                    #     # Obtener la transformación de perspectiva
                    #     warped_image = get_perspective_transformed_image(roi, sorted_vertices)

                    # else: # Si no se detectan los bordes externos (suponiendo que son los que más sobresalen en el documento de identificación)
                    #     # Obtener la transformación de perspectiva sin bordes detectados
                    #     warped_image = get_perspective_transformed_image(roi)
                    #     sorted_vertices = None  # Indicar que no hay vértices ordenados

                    # Obtener recorte del documento identificado sin bordes detectados
                    warped_image = get_perspective_transformed_image(roi)

                    # Guardar la imagen recortada
                    save_warped_path = str(cropped_dir / (p.stem + f'_{i}_warped.jpg'))
                    cv2.imwrite(save_warped_path, warped_image)

                    # Print results
                    if save_txt:  # Write to file
                        xywh = (xyxy2xywh(torch.tensor(xyxy).view(1, 4)) / gn).view(-1).tolist()  # normalized xywh
                        line = (cls, *xywh, conf) if opt.save_conf else (cls, *xywh)  # label format
                        with open(txt_path + '.txt', 'a') as f:
                            f.write(('%g ' * len(line)).rstrip() % line + '\n')

                    if save_img or view_img:  # Add bbox to image
                        label = f'{names[int(cls)]} {conf:.2f}'
                        plot_one_box(xyxy, im0, label=label, color=colors[int(cls)], line_thickness=1)

            # Print time (inference + NMS)
            print(f'{s}Done. ({(1E3 * (t2 - t1)):.1f}ms) Inference, ({(1E3 * (t3 - t2)):.1f}ms) NMS')

            # Stream results
            if view_img:
                cv2.imshow(str(p), im0)
                cv2.waitKey(1)  # 1 millisecond

            # Save results (image with detections)
            if save_img:
                if dataset.mode == 'image':
                    cv2.imwrite(save_path, im0)
                    print(f" The image with the result is saved in: {save_path}")
                else:  # 'video' or 'stream'
                    if vid_path != save_path:  # new video
                        vid_path = save_path
                        if isinstance(vid_writer, cv2.VideoWriter):
                            vid_writer.release()  # release previous video writer
                        if vid_cap:  # video
                            fps = vid_cap.get(cv2.CAP_PROP_FPS)
                            w = int(vid_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                            h = int(vid_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        else:  # stream
                            fps, w, h = 30, im0.shape[1], im0.shape[0]
                            save_path += '.mp4'
                        vid_writer = cv2.VideoWriter(save_path, cv2.VideoWriter_fourcc(*'mp4v'), fps, (w, h))
                    vid_writer.write(im0)

    if save_txt or save_img:
        s = f"\n{len(list(save_dir.glob('labels/*.txt')))} labels saved to {save_dir / 'labels'}" if save_txt else ''
        #print(f"Results saved to {save_dir}{s}")

    print(f'Done. ({time.time() - t0:.3f}s)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--weights', nargs='+', type=str, default='yolov7.pt', help='model.pt path(s)')
    parser.add_argument('--source', type=str, default='inference/images', help='source')  # file/folder, 0 for webcam
    parser.add_argument('--img-size', type=int, default=640, help='inference size (pixels)')
    parser.add_argument('--conf-thres', type=float, default=0.25, help='object confidence threshold')
    parser.add_argument('--iou-thres', type=float, default=0.45, help='IOU threshold for NMS')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--view-img', action='store_true', help='display results')
    parser.add_argument('--save-txt', action='store_true', help='save results to *.txt')
    parser.add_argument('--save-conf', action='store_true', help='save confidences in --save-txt labels')
    parser.add_argument('--nosave', action='store_true', help='do not save images/videos')
    parser.add_argument('--classes', nargs='+', type=int, help='filter by class: --class 0, or --class 0 2 3')
    parser.add_argument('--agnostic-nms', action='store_true', help='class-agnostic NMS')
    parser.add_argument('--augment', action='store_true', help='augmented inference')
    parser.add_argument('--update', action='store_true', help='update all models')
    parser.add_argument('--project', default='runs/detect', help='save results to project/name')
    parser.add_argument('--name', default='exp', help='save results to project/name')
    parser.add_argument('--exist-ok', action='store_true', help='existing project/name ok, do not increment')
    parser.add_argument('--no-trace', action='store_true', help='don`t trace model')
    parser.add_argument('--offset', type=int, default=20, help='offset value in roi detected in bounding box (makes bbox bigger)')
    parser.add_argument('--edge-enhancer', type=int, default=2, help='edge-enhancer list laplacian (0) sobel (1) canny(2)')
    opt = parser.parse_args()
    print(opt)
    #check_requirements(exclude=('pycocotools', 'thop'))

    with torch.no_grad():
        if opt.update:  # update all models (to fix SourceChangeWarning)
            for opt.weights in ['yolov7.pt']:
                detect()
                strip_optimizer(opt.weights)
        else:
            detect()
